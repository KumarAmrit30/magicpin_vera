#!/usr/bin/env python3
"""Offline, development-only LLM judge for Vera's canonical messages (diagnostic, not the official score).

Never imported by the application. It runs only when invoked explicitly::

    # 1. freeze the inputs once (the generated dataset, per case, with hashes); refuses to overwrite
    .venv/bin/python scripts/llm_judge.py freeze --output reports/phase-3d/inputs/cases.json

    # 2. run the current production code against exactly those inputs
    .venv/bin/python scripts/llm_judge.py run --cases reports/phase-3d/inputs/cases.json \
        --output reports/phase-3d/before/outputs.json

    # 3. judge (live: the key is read from VERA_EVAL_API_KEY in the calling shell only)
    .venv/bin/python scripts/llm_judge.py judge --cases reports/phase-3d/inputs/cases.json \
        --outputs reports/phase-3d/before/outputs.json --provider gemini --model gemini-3.5-flash \
        --output reports/phase-3d/before/judgements.json [--only T28]

    # 4. human-readable report / before-after comparison (no network)
    .venv/bin/python scripts/llm_judge.py report --outputs … --judgements … --output report.md
    .venv/bin/python scripts/llm_judge.py compare --before-outputs … --after-outputs … \
        [--before-judgements … --after-judgements …] --output compare.md

Scores are per dimension and diagnostic only: there is no combined score and no ranking of cases.
The API key is never printed, logged or written; request headers are never stored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent

DIMENSIONS = ("specificity", "category_fit", "merchant_fit", "trigger_relevance", "engagement_compulsion")
"""challenge-brief.md §8, in its order and with its names."""

DIMENSION_TITLES = {
    "specificity": "Specificity", "category_fit": "Category fit", "merchant_fit": "Merchant fit",
    "trigger_relevance": "Trigger relevance", "engagement_compulsion": "Engagement compulsion",
}
CHECKS = ("groundedness", "rationale_alignment", "cta_assessment")
CHECK_TITLES = {"groundedness": "Groundedness", "rationale_alignment": "Rationale alignment", "cta_assessment": "CTA assessment"}

DECISION_FIELDS = ("outcome", "action", "scope", "selected_offer_id", "plan_id", "suppression_key",
                   "plan_suppression_key", "send_as", "cta", "rationale")
"""Fields the frozen decision layer owns; any before/after difference is a regression."""

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
RUBRIC_VERSION = "phase-3d-v1"

SYSTEM_PROMPT = """\
You are a strict, independent evaluator of WhatsApp messages written by "Vera", magicpin's assistant for
Indian local merchants (dentists, salons, restaurants, gyms, pharmacies). This is an offline DIAGNOSTIC
evaluation, not a competition score. Evaluate the single case you are given on its own; never compare it
with other cases, never rank it, never call it best or worst.

You receive the contexts Vera had (category, merchant, optional customer, trigger) and the message Vera
would actually send: action, scope, cta, send_as, body and rationale, plus the context fields Vera says the
body uses. send_as "vera" means Vera writes to the merchant owner; "merchant_on_behalf" means the message is
sent from the merchant's own number to their customer. cta "binary_confirm_cancel" expects a CONFIRM reply,
"binary_yes_no" a yes/no reply, "open_ended" a free reply, "none" no reply.

Score each dimension 0-10 (integers). Use the challenge's dimensions exactly:

1. specificity - Does the message anchor on concrete, verifiable facts already present in the supplied
   context (real numbers, dates, offers, customer information, merchant metrics, deadlines, availability,
   relevant sources)? Penalize generic wording when useful facts exist, and any unsupported number,
   invented date, invented offer or fabricated availability. Never reward fabrication.
2. category_fit - Do the voice, vocabulary and offer format fit the merchant's category and this trigger
   (e.g. dentist messages clinical-peer, not retail-promo)? A generic sentence does not earn full credit
   merely because it is grammatically correct.
3. merchant_fit - Is the message about THIS merchant (and customer, if any): their name, owner name,
   metrics, offers, locality, customer facts, conversation state? Is the language preference honored
   (challenge FAQ: match the merchant's identity.languages, default English, Hindi-English code-mix is
   encouraged where the language pref says hi; for customer-facing messages the customer's
   language_pref applies)? Generic category language is not merchant-specific.
4. trigger_relevance - Does the message clearly communicate why now: the specific trigger that prompted
   it (appointment reminder -> appointment, refill -> refill, performance dip -> the signal, opportunity ->
   the opportunity, compliance -> the compliance issue)? Treat the selected action as given; judge how
   well the message expresses it.
5. engagement_compulsion - Does the message make a reasonable next response easy: one clear, relevant,
   low-effort CTA that matches the action, concise, clear next step? Penalize multiple competing CTAs,
   vague CTAs, unnecessary questions, CTAs unrelated to the body, high-friction replies. Do not reward
   manipulative pressure.

Also assess:

- groundedness (0-10): check EVERY concrete factual claim in the body against the supplied context. A
  real number is not automatically good: does this exact value exist in the context, for the same entity,
  the same metric, the same period and direction? Total customers are not "affected customers"; total
  orders are not "orders from this campaign"; a category or peer metric is not the merchant's own metric.
  List each unsupported or mis-attributed claim verbatim in unsupported_claims. List in
  missing_context_used useful context facts the body could have used but did not (only facts that are
  actually present in the context).
- rationale_alignment (0-10): does the rationale describe what the body actually does? List mismatches.
- cta_assessment (0-10): is there exactly one CTA, in the last sentence, matching the cta type and action?
  List problems.

critical_issues: problems a recipient would notice as wrong or harmful (fabrication, wrong entity, wrong
language for an explicit preference, contradiction). suggested_improvements: concrete wording changes that
use only facts present in the context. Never suggest inventing facts.

Return ONLY one JSON object, no markdown, exactly this shape (all keys required, no extra keys; every list
contains only strings; every score is an integer 0-10):
{"case_id": "<echo the case_id>",
 "dimensions": {"specificity": {"score": 0, "reason": ""}, "category_fit": {"score": 0, "reason": ""},
  "merchant_fit": {"score": 0, "reason": ""}, "trigger_relevance": {"score": 0, "reason": ""},
  "engagement_compulsion": {"score": 0, "reason": ""}},
 "groundedness": {"score": 0, "unsupported_claims": [], "missing_context_used": []},
 "rationale_alignment": {"score": 0, "issues": []},
 "cta_assessment": {"score": 0, "issues": []},
 "critical_issues": [],
 "suggested_improvements": []}
"""


class JudgeConfigError(RuntimeError):
    """The judge cannot start (missing credentials, unknown provider, bad model name)."""


class ProviderError(RuntimeError):
    """The provider call failed; the message is already redacted."""


class JudgeResponseError(ValueError):
    """The provider answered, but not with the required strict JSON."""


class InputMismatchError(RuntimeError):
    """Inputs or outputs do not belong to the same frozen case file."""


# --------------------------------------------------------------------------- #
# Hashing and files
# --------------------------------------------------------------------------- #


def sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write_new(path: Path, payload: Any) -> None:
    """Write JSON, refusing to replace an existing file (frozen inputs and live results are immutable)."""
    if path.exists():
        raise FileExistsError(f"{path} already exists; it is immutable. Choose a new --output path.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git_state() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()

    changed = [line[3:] for line in git("status", "--porcelain").splitlines() if line]
    return {"commit": git("rev-parse", "HEAD") or None, "changed_paths": changed}


def _harness() -> Any:
    """``scripts/evaluate_vera.py``, imported only by the steps that run Vera."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import evaluate_vera

    return evaluate_vera


# --------------------------------------------------------------------------- #
# Frozen inputs
# --------------------------------------------------------------------------- #


def freeze_cases(data: dict[str, dict[str, dict[str, Any]]], cases: list[tuple[str, str]], now: datetime,
                 source: str) -> dict[str, Any]:
    """One entry per case with exactly the contexts Vera receives, each hashed."""
    contexts_for = _harness().contexts_for
    frozen = []
    for case_id, trigger_id in cases:
        case = {
            "case_id": case_id, "trigger_id": trigger_id, "now": now.isoformat(),
            "contexts": [{"scope": s, "context_id": cid, "payload": payload} for s, cid, payload in contexts_for(data, trigger_id)],
        }
        case["input_sha256"] = sha256_json(case)
        frozen.append(case)
    return {"version": 1, "source": source, "created_at": utcnow(), "now": now.isoformat(),
            "cases": frozen, "cases_sha256": sha256_json([c["input_sha256"] for c in frozen])}


def load_cases(path: Path) -> dict[str, Any]:
    """Load a frozen case file and verify every hash; a hand-edited file is rejected."""
    frozen = json.loads(path.read_text())
    for case in frozen["cases"]:
        body = {k: v for k, v in case.items() if k != "input_sha256"}
        if sha256_json(body) != case["input_sha256"]:
            raise InputMismatchError(f"{path}: case {case['case_id']} does not match its input_sha256 (inputs were modified)")
    if sha256_json([c["input_sha256"] for c in frozen["cases"]]) != frozen["cases_sha256"]:
        raise InputMismatchError(f"{path}: cases_sha256 does not match the cases (inputs were modified)")
    return frozen


def run_cases(frozen: dict[str, Any]) -> dict[str, Any]:
    """Vera's real output (fresh in-process app per case) for exactly the frozen contexts."""
    harness = _harness()
    rows = []
    for case in frozen["cases"]:
        data: dict[str, dict[str, dict[str, Any]]] = {"category": {}, "merchant": {}, "customer": {}, "trigger": {}}
        for item in case["contexts"]:
            data[item["scope"]][item["context_id"]] = item["payload"]
        now = datetime.fromisoformat(case["now"])
        row = harness.evaluate_cases(data, [(case["case_id"], case["trigger_id"])], now)[0]
        row["input_sha256"] = case["input_sha256"]
        rows.append(row)
    return {"version": 1, "created_at": utcnow(), "git": git_state(), "cases_sha256": frozen["cases_sha256"], "rows": rows}


def check_outputs_match(frozen: dict[str, Any], outputs: dict[str, Any], label: str = "outputs") -> None:
    if outputs.get("cases_sha256") != frozen["cases_sha256"]:
        raise InputMismatchError(f"{label} were produced from a different case file (cases_sha256 differs)")
    expected = {c["case_id"]: c["input_sha256"] for c in frozen["cases"]}
    for row in outputs["rows"]:
        if expected.get(row["case_id"]) != row.get("input_sha256"):
            raise InputMismatchError(f"{label}: case {row['case_id']} was produced from different inputs")


# --------------------------------------------------------------------------- #
# Judge input
# --------------------------------------------------------------------------- #


def resolve_ref(ref: str, contexts: dict[str, Any]) -> Any:
    """``merchant:offers.0.title`` -> the value in the frozen merchant payload (None if absent)."""
    source, _, path = ref.partition(":")
    value: Any = contexts.get(source)
    for part in path.split(".") if path else ():
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def build_judge_input(case: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """What the judge sees: the contexts and the message as sent. No candidates, scores or harness metrics."""
    contexts = {item["scope"]: item["payload"] for item in case["contexts"]}
    return {
        "case_id": case["case_id"],
        "category": contexts.get("category"),
        "merchant": contexts.get("merchant"),
        "customer": contexts.get("customer"),
        "trigger": contexts.get("trigger"),
        "vera_output": {field: row.get(field) for field in ("action", "scope", "cta", "send_as", "body", "rationale")},
        "facts_used": [{"ref": ref, "value": resolve_ref(ref, contexts)} for ref in (row.get("metrics") or {}).get("facts_used", [])],
    }


def judge_prompt(judge_input: dict[str, Any]) -> str:
    return "Evaluate this case. Input JSON:\n" + json.dumps(judge_input, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------- #
# Strict schema
# --------------------------------------------------------------------------- #


def _score(value: Any, where: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10:
        raise JudgeResponseError(f"{where}: score must be an integer 0-10, got {value!r}")


def _strings(value: Any, where: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise JudgeResponseError(f"{where}: must be a list of strings")


def _keys(value: Any, expected: set[str], where: str) -> None:
    if not isinstance(value, dict):
        raise JudgeResponseError(f"{where}: must be an object")
    if set(value) != expected:
        missing, extra = sorted(expected - set(value)), sorted(set(value) - expected)
        raise JudgeResponseError(f"{where}: keys differ from the schema (missing={missing}, unexpected={extra})")


def _root(data: Any, case_id: str) -> None:
    _keys(data, {"case_id", "dimensions", *CHECKS, "critical_issues", "suggested_improvements"}, "root")
    if data["case_id"] != case_id:
        raise JudgeResponseError(f"case_id {data['case_id']!r} does not echo {case_id!r}")


def _dimensions(data: dict[str, Any]) -> None:
    _keys(data["dimensions"], set(DIMENSIONS), "dimensions")
    for name in DIMENSIONS:
        _keys(data["dimensions"][name], {"score", "reason"}, f"dimensions.{name}")
        _score(data["dimensions"][name]["score"], f"dimensions.{name}")
        if not isinstance(data["dimensions"][name]["reason"], str):
            raise JudgeResponseError(f"dimensions.{name}.reason must be a string")


def _check_object(data: dict[str, Any], name: str, lists: tuple[str, ...]) -> None:
    _keys(data[name], {"score", *lists}, name)
    _score(data[name]["score"], name)
    for field in lists:
        _strings(data[name][field], f"{name}.{field}")


def schema_checks(raw: str, case_id: str) -> list[tuple[str, bool, str]]:
    """Each smoke-test check separately: (name, passed, detail). Later checks are skipped once one fails."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [("valid JSON", False, str(exc))]
    steps: list[tuple[str, Callable[[], None]]] = [
        ("strict top-level schema", lambda: _root(data, case_id)),
        ("five dimensions, integer scores 0-10 with reasons", lambda: _dimensions(data)),
        ("groundedness object", lambda: _check_object(data, "groundedness", ("unsupported_claims", "missing_context_used"))),
        ("rationale_alignment object", lambda: _check_object(data, "rationale_alignment", ("issues",))),
        ("cta_assessment object", lambda: _check_object(data, "cta_assessment", ("issues",))),
        ("critical_issues / suggested_improvements lists", lambda: (
            _strings(data["critical_issues"], "critical_issues"), _strings(data["suggested_improvements"], "suggested_improvements"))),
    ]
    results = [("valid JSON", True, "")]
    for name, check in steps:
        try:
            check()
        except JudgeResponseError as exc:
            results.append((name, False, str(exc)))
            return results
        results.append((name, True, ""))
    return results


def validate_judgement(raw: str, case_id: str) -> dict[str, Any]:
    """The parsed judgement, or JudgeResponseError naming the first schema violation."""
    for name, passed, detail in schema_checks(raw, case_id):
        if not passed:
            raise JudgeResponseError(f"{name}: {detail}")
    return json.loads(raw)


# --------------------------------------------------------------------------- #
# Providers (evaluation only)
# --------------------------------------------------------------------------- #


class Provider:
    name = "provider"
    model = ""

    def complete(self, system: str, prompt: str, case_id: str) -> str:
        raise NotImplementedError


class GeminiProvider(Provider):
    """Google Generative Language ``generateContent``; the key travels only in the ``x-goog-api-key`` header."""

    name = "gemini"

    def __init__(self, api_key: str, model: str, temperature: float = 0.0, timeout: float = 180.0,
                 opener: Callable[..., Any] = urlrequest.urlopen) -> None:
        if not MODEL_NAME.fullmatch(model):
            raise JudgeConfigError(f"invalid model name {model!r}")
        self._key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self._open = opener

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "[REDACTED]") if self._key else text

    def complete(self, system: str, prompt: str, case_id: str) -> str:
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": self.temperature, "responseMimeType": "application/json"},
        }).encode()
        req = urlrequest.Request(f"{GEMINI_BASE}/models/{self.model}:generateContent", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": self._key})
        try:
            with self._open(req, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode())
        except urlerror.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("error", {}).get("message", detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            raise ProviderError(self._redact(f"HTTP {exc.code}: {detail}")) from None
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(self._redact(f"request failed: {exc}")) from None
        except json.JSONDecodeError as exc:
            raise ProviderError(f"provider returned a non-JSON envelope: {exc}") from None

        candidates = payload.get("candidates") or []
        if not candidates:
            raise ProviderError(self._redact(f"no candidates returned (promptFeedback={payload.get('promptFeedback')})"))
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise ProviderError(f"empty response (finishReason={candidates[0].get('finishReason')})")
        return text


class FixtureProvider(Provider):
    """Stored judge responses by case id, for deterministic tests; a missing case is an error, never a default."""

    name = "fixture"

    def __init__(self, path: Path) -> None:
        self.model = f"fixture:{path.name}"
        self._responses = json.loads(path.read_text())["responses"]

    def complete(self, system: str, prompt: str, case_id: str) -> str:
        if case_id not in self._responses:
            raise ProviderError(f"no stored response for case {case_id}")
        stored = self._responses[case_id]
        return stored if isinstance(stored, str) else json.dumps(stored)


def create_provider(name: str | None, model: str | None, env: dict[str, str], fixtures: Path | None = None,
                    temperature: float = 0.0) -> Provider:
    """The requested provider only; there is no fallback to another provider or to stored scores."""
    name = name or env.get("VERA_EVAL_PROVIDER")
    if name == "gemini":
        key = env.get("VERA_EVAL_API_KEY", "")
        if not key.strip():
            raise JudgeConfigError("VERA_EVAL_API_KEY is not set in this shell; export it before running the live judge "
                                   "(it is read from the environment only and never written anywhere)")
        model = model or env.get("VERA_EVAL_MODEL")
        if not model:
            raise JudgeConfigError("no model given: pass --model or set VERA_EVAL_MODEL")
        return GeminiProvider(key.strip(), model, temperature=temperature)
    if name == "fixture":
        if fixtures is None:
            raise JudgeConfigError("--fixtures is required for the fixture provider")
        return FixtureProvider(fixtures)
    raise JudgeConfigError(f"unknown provider {name!r}; available: gemini, fixture")


# --------------------------------------------------------------------------- #
# Judging
# --------------------------------------------------------------------------- #


def judge_cases(frozen: dict[str, Any], outputs: dict[str, Any], provider: Provider, only: list[str] | None = None,
                retries: int = 1, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Judge every emitted case (or ``only``). Failures are recorded as errors; scores are never substituted."""
    check_outputs_match(frozen, outputs)
    cases = {c["case_id"]: c for c in frozen["cases"]}
    if only:
        unknown = sorted(set(only) - set(cases))
        if unknown:
            raise InputMismatchError(f"unknown case ids: {unknown}")
    started = utcnow()
    results = []
    aborted = None
    for row in outputs["rows"]:
        case_id = row["case_id"]
        if only and case_id not in only:
            continue
        base = {"case_id": case_id, "input_sha256": row["input_sha256"]}
        if not row.get("emitted"):
            results.append(base | {"status": "not_emitted", "outcome": row.get("outcome")})
            continue
        base["body_sha256"] = sha256_text(row["body"])
        prompt = judge_prompt(build_judge_input(cases[case_id], row))
        attempts: list[str] = []
        for attempt in range(retries + 1):
            try:
                raw = provider.complete(SYSTEM_PROMPT, prompt, case_id)
            except ProviderError as exc:
                aborted = f"{case_id}: {exc}"
                results.append(base | {"status": "provider_error", "error": str(exc)})
                log(f"[FAIL] {case_id} request: {exc}")
                break
            log(f"[PASS] {case_id} request succeeded (attempt {attempt + 1})")
            checks = schema_checks(raw, case_id)
            for name, passed, detail in checks:
                log(f"[{'PASS' if passed else 'FAIL'}] {case_id} {name}" + (f": {detail}" if detail else ""))
            failed = next((f"{name}: {detail}" for name, passed, detail in checks if not passed), None)
            if failed is None:
                results.append(base | {"status": "ok", "attempts": attempt + 1, "judgement": json.loads(raw)})
                break
            attempts.append(failed)
        else:
            results.append(base | {"status": "judge_error", "errors": attempts})
        if aborted:
            break
        if isinstance(provider, GeminiProvider):
            time.sleep(1.0)
    meta = {
        "rubric_version": RUBRIC_VERSION, "provider": provider.name, "model": provider.model,
        "temperature": getattr(provider, "temperature", None), "system_prompt_sha256": sha256_text(SYSTEM_PROMPT),
        "started_at": started, "finished_at": utcnow(), "git": git_state(),
        "only": only, "aborted": aborted,
    }
    return {"version": 1, "meta": meta, "cases_sha256": frozen["cases_sha256"],
            "outputs_sha256": sha256_json(outputs["rows"]), "results": results}


JUDGE_IDENTITY = ("rubric_version", "provider", "model", "temperature", "system_prompt_sha256")
"""A merged run may only combine artifacts judged by the same judge with the same prompt."""


def merge_judgements(frozen: dict[str, Any], outputs: dict[str, Any], artifacts: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """One judged run from several partial runs (e.g. a run that aborted plus per-case retries), offline.

    Every emitted case needs exactly one ``ok`` result; a failed attempt superseded by an ``ok`` elsewhere is
    kept in ``meta.superseded``. Judgements are copied verbatim after re-validating them against the schema.
    """
    check_outputs_match(frozen, outputs)
    if not artifacts:
        raise InputMismatchError("no judgement artifacts given")
    outputs_sha = sha256_json(outputs["rows"])
    first = artifacts[0][1]["meta"]
    rows = {r["case_id"]: r for r in outputs["rows"]}
    ok: dict[str, tuple[str, dict[str, Any]]] = {}
    failed: list[dict[str, Any]] = []
    for label, judged in artifacts:
        if judged["cases_sha256"] != frozen["cases_sha256"] or judged["outputs_sha256"] != outputs_sha:
            raise InputMismatchError(f"{label} was not judged from these inputs and outputs")
        for field in JUDGE_IDENTITY:
            if judged["meta"][field] != first[field]:
                raise InputMismatchError(f"{label}: {field} {judged['meta'][field]!r} differs from {first[field]!r}")
        for result in judged["results"]:
            case_id = result["case_id"]
            row = rows.get(case_id)
            if row is None or result["input_sha256"] != row["input_sha256"]:
                raise InputMismatchError(f"{label}: case {case_id} does not belong to these inputs")
            if result["status"] == "not_emitted":
                if row.get("emitted"):
                    raise InputMismatchError(f"{label}: case {case_id} is marked not emitted but was emitted")
                continue
            if result["status"] != "ok":
                failed.append({"case_id": case_id, "status": result["status"], "from": label,
                               "error": result.get("error") or result.get("errors")})
                continue
            if case_id in ok:
                raise InputMismatchError(f"case {case_id} has a judgement in both {ok[case_id][0]} and {label}")
            if result.get("body_sha256") != sha256_text(row["body"]):
                raise InputMismatchError(f"{label}: case {case_id} was judged on a different body")
            validate_judgement(json.dumps(result["judgement"]), case_id)
            ok[case_id] = (label, result)

    missing = [c for c, r in rows.items() if r.get("emitted") and c not in ok]
    if missing:
        raise InputMismatchError(f"emitted cases without a valid judgement: {missing}")
    results = [ok[r["case_id"]][1] if r.get("emitted") else
               {"case_id": r["case_id"], "input_sha256": r["input_sha256"], "status": "not_emitted", "outcome": r.get("outcome")}
               for r in outputs["rows"]]
    metas = [judged["meta"] for _, judged in artifacts]
    meta = {field: first[field] for field in JUDGE_IDENTITY} | {
        "started_at": min(m["started_at"] for m in metas), "finished_at": max(m["finished_at"] for m in metas),
        "git": first["git"], "only": None, "aborted": None, "merged_at": utcnow(),
        "merged_from": [{"artifact": label, "judged_cases": [r["case_id"] for r in judged["results"]
                                                            if r["status"] == "ok" and ok[r["case_id"]][0] == label]}
                        for label, judged in artifacts],
        "superseded": failed,
    }
    return {"version": 1, "meta": meta, "cases_sha256": frozen["cases_sha256"], "outputs_sha256": outputs_sha, "results": results}


# --------------------------------------------------------------------------- #
# Reports (no combined score, no ranking)
# --------------------------------------------------------------------------- #

DISCLAIMER = ("Diagnostic LLM evaluation (offline, development only). This is not the official challenge score; "
              "scores are per dimension, never combined, and cases are listed in case order, not ranked.")


def _score_of(judgement: dict[str, Any], key: str) -> int:
    return judgement["dimensions"][key]["score"] if key in DIMENSIONS else judgement[key]["score"]


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def _distribution(judgements: list[dict[str, Any]], key: str) -> str:
    counts = Counter(_score_of(j, key) for j in judgements)
    return " | ".join(str(counts.get(score, 0)) for score in range(11))


def render_report(outputs: dict[str, Any], judged: dict[str, Any]) -> str:
    rows = {r["case_id"]: r for r in outputs["rows"]}
    ok = [r for r in judged["results"] if r["status"] == "ok"]
    meta = judged["meta"]
    lines = [f"# Vera LLM evaluation — {meta['provider']} / {meta['model']}", "", f"> {DISCLAIMER}", "",
             f"- rubric `{meta['rubric_version']}`, system prompt sha256 `{meta['system_prompt_sha256'][:16]}`, "
             f"temperature {meta['temperature']}, run {meta['started_at']} → {meta['finished_at']}",
             f"- inputs `cases_sha256={judged['cases_sha256'][:16]}`; outputs commit `{(outputs.get('git') or {}).get('commit')}`",
             f"- judged: {len(ok)}; not emitted: {sum(r['status'] == 'not_emitted' for r in judged['results'])}; "
             f"errors: {sum(r['status'] in ('judge_error', 'provider_error') for r in judged['results'])}"
             + (f"; aborted: {meta['aborted']}" if meta.get("aborted") else ""), "",
             "## Score counts per dimension", "",
             "| Dimension | " + " | ".join(str(s) for s in range(11)) + " |", "|---|" + "---|" * 11]
    for key in (*DIMENSIONS, *CHECKS):
        title = DIMENSION_TITLES.get(key) or CHECK_TITLES[key]
        lines.append(f"| {title} | {_distribution([r['judgement'] for r in ok], key) if ok else ''} |")
    if meta.get("merged_from"):
        lines += ["", "Merged from: " + "; ".join(f"`{m['artifact']}` ({', '.join(m['judged_cases']) or 'no judged cases'})"
                                                  for m in meta["merged_from"])]
        lines += [f"Superseded attempt: {s['case_id']} {s['status']} in `{s['from']}`: {s['error']}" for s in meta["superseded"]]
    if ok:
        lines += ["", "## Mean score per dimension (each dimension separately)", "",
                  "| " + " | ".join(DIMENSION_TITLES.get(k) or CHECK_TITLES[k] for k in (*DIMENSIONS, *CHECKS)) + " |",
                  "|" + "---|" * (len(DIMENSIONS) + len(CHECKS)),
                  "| " + " | ".join(f"{sum(_score_of(r['judgement'], k) for r in ok) / len(ok):.1f}" for k in (*DIMENSIONS, *CHECKS)) + " |",
                  "", "## Scores per case (case order)", "",
                  "| Case | " + " | ".join(DIMENSION_TITLES.get(k) or CHECK_TITLES[k] for k in (*DIMENSIONS, *CHECKS)) + " |",
                  "|---|" + "---|" * (len(DIMENSIONS) + len(CHECKS))]
        lines += [f"| {r['case_id']} | " + " | ".join(str(_score_of(r["judgement"], k)) for k in (*DIMENSIONS, *CHECKS)) + " |"
                  for r in ok]
    lines += ["", "## Cases (case order)", ""]
    for result in judged["results"]:
        row = rows.get(result["case_id"], {})
        lines.append(f"### {result['case_id']} — {row.get('trigger_kind')} / {row.get('category')} — "
                     f"{row.get('action')} ({row.get('scope')}, {row.get('cta')})")
        if result["status"] != "ok":
            lines += [f"- status: {result['status']} {result.get('error') or result.get('errors') or result.get('outcome') or ''}", ""]
            continue
        j, m = result["judgement"], row.get("metrics") or {}
        lines += [f"> {row.get('body')}", "", f"- rationale: `{row.get('rationale')}`"]
        for key in DIMENSIONS:
            lines.append(f"- **{DIMENSION_TITLES[key]}** {j['dimensions'][key]['score']}: {j['dimensions'][key]['reason']}")
        g = j["groundedness"]
        lines.append(f"- **Groundedness** {g['score']}; unsupported: {g['unsupported_claims'] or 'none'}; "
                     f"unused context: {g['missing_context_used'] or 'none'}")
        lines.append(f"- **Rationale alignment** {j['rationale_alignment']['score']}: {j['rationale_alignment']['issues'] or 'no issues'}")
        lines.append(f"- **CTA assessment** {j['cta_assessment']['score']}: {j['cta_assessment']['issues'] or 'no issues'}")
        if j["critical_issues"]:
            lines.append(f"- critical issues: {j['critical_issues']}")
        if j["suggested_improvements"]:
            lines.append(f"- suggested improvements: {j['suggested_improvements']}")
        lines.append(f"- deterministic precheck: numbers not in contexts {m.get('numbers_not_found_in_contexts')}, leaks "
                     f"{m.get('leaks')}, CTA count {m.get('cta_count')}, CTA last {m.get('cta_in_last_sentence')}, "
                     f"language match {m.get('language_match')}, grounded not rendered {m.get('grounded_not_rendered')}")
        lines.append("")
    return "\n".join(lines)


def decision_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    old = {r["case_id"]: r for r in before["rows"]}
    changes = []
    for row in after["rows"]:
        prior = old.get(row["case_id"])
        if prior is None:
            changes.append(f"{row['case_id']}: missing from the before outputs")
            continue
        changes += [f"{row['case_id']}: {field} {prior.get(field)!r} -> {row.get(field)!r}"
                    for field in DECISION_FIELDS if prior.get(field) != row.get(field)]
    if set(old) != {r["case_id"] for r in after["rows"]}:
        changes.append("case sets differ")
    return changes


def render_compare(before: dict[str, Any], after: dict[str, Any], before_judged: dict[str, Any] | None,
                   after_judged: dict[str, Any] | None) -> tuple[str, list[str]]:
    """Per case and dimension, before vs after; unchanged bodies are marked so judge variance is visible."""
    if before["cases_sha256"] != after["cases_sha256"]:
        raise InputMismatchError("before and after outputs come from different case files")
    for judged, outputs, label in ((before_judged, before, "before"), (after_judged, after, "after")):
        if judged is not None and (judged["cases_sha256"] != outputs["cases_sha256"]
                                   or judged["outputs_sha256"] != sha256_json(outputs["rows"])):
            raise InputMismatchError(f"{label} judgements were not produced from the {label} outputs")
    inputs_b = {r["case_id"]: r["input_sha256"] for r in before["rows"]}
    inputs_a = {r["case_id"]: r["input_sha256"] for r in after["rows"]}
    if inputs_b != inputs_a:
        raise InputMismatchError("per-case inputs differ between before and after")

    changes = decision_changes(before, after)
    jb = {r["case_id"]: r for r in (before_judged or {}).get("results", [])}
    ja = {r["case_id"]: r for r in (after_judged or {}).get("results", [])}
    old = {r["case_id"]: r for r in before["rows"]}
    lines = ["# Vera before/after comparison", "", f"> {DISCLAIMER}", "",
             f"- identical inputs: `cases_sha256={before['cases_sha256'][:16]}` for both runs; every case's input_sha256 matches",
             f"- before commit `{before['git'].get('commit')}` (changed paths {len(before['git'].get('changed_paths', []))}); "
             f"after commit `{after['git'].get('commit')}` (changed paths {len(after['git'].get('changed_paths', []))})",
             f"- judges: before `{(before_judged or {}).get('meta', {}).get('model')}`, after `{(after_judged or {}).get('meta', {}).get('model')}`",
             "", "## Decision integrity", ""]
    lines += [f"- DECISION FIELD CHANGED: {c}" for c in changes] or [f"- 0 decision-field changes across {len(after['rows'])} cases ({', '.join(DECISION_FIELDS)})"]
    changed_bodies = [r["case_id"] for r in after["rows"] if r.get("body") != old[r["case_id"]].get("body")]
    lines += ["", f"## Bodies changed: {len(changed_bodies)} ({', '.join(changed_bodies) or 'none'})", ""]
    for row in after["rows"]:
        case_id, prior = row["case_id"], old[row["case_id"]]
        if not row.get("emitted") and not prior.get("emitted"):
            continue
        changed = case_id in changed_bodies
        lines.append(f"### {case_id} — {row.get('trigger_kind')} — {'body changed' if changed else 'body unchanged (score differences are judge variance)'}")
        if changed:
            lines += [f"- before: {prior.get('body')}", f"- after: {row.get('body')}"]
        mb, ma = prior.get("metrics") or {}, row.get("metrics") or {}
        lines.append(f"- grounding (deterministic): numbers not in contexts {mb.get('numbers_not_found_in_contexts')} → "
                     f"{ma.get('numbers_not_found_in_contexts')}; leaks {mb.get('leaks')} → {ma.get('leaks')}")
        lines.append(f"- CTA: {prior.get('cta')} / count {mb.get('cta_count')} / last {mb.get('cta_in_last_sentence')} → "
                     f"{row.get('cta')} / count {ma.get('cta_count')} / last {ma.get('cta_in_last_sentence')}")
        b, a = jb.get(case_id), ja.get(case_id)
        if b and a and b["status"] == a["status"] == "ok":
            bj, aj = b["judgement"], a["judgement"]
            lines += ["", "| Dimension | Before | After | Before observation | After observation |", "|---|---|---|---|---|"]
            for key in (*DIMENSIONS, *CHECKS):
                title = DIMENSION_TITLES.get(key) or CHECK_TITLES[key]
                if key in DIMENSIONS:
                    ob, oa = bj["dimensions"][key]["reason"], aj["dimensions"][key]["reason"]
                elif key == "groundedness":
                    ob, oa = "; ".join(bj[key]["unsupported_claims"]) or "no unsupported claims", "; ".join(aj[key]["unsupported_claims"]) or "no unsupported claims"
                else:
                    ob, oa = "; ".join(bj[key]["issues"]) or "no issues", "; ".join(aj[key]["issues"]) or "no issues"
                lines.append(f"| {title} | {_score_of(bj, key)} | {_score_of(aj, key)} | {_cell(ob)} | {_cell(oa)} |")
        elif b or a:
            lines.append(f"- judge status: before {b and b['status']}, after {a and a['status']}")
        lines.append("")
    return "\n".join(lines), changes


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("freeze", help="generate the canonical dataset once and freeze the per-case inputs")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--set", choices=("canonical", "seed"), default="canonical")

    p = sub.add_parser("run", help="run the current production code against a frozen case file")
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)

    p = sub.add_parser("judge", help="send the outputs to the LLM judge")
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--outputs", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--provider", choices=("gemini", "fixture"))
    p.add_argument("--model")
    p.add_argument("--fixtures", type=Path)
    p.add_argument("--only", nargs="+", help="case ids to judge (e.g. T28 for the smoke test)")
    p.add_argument("--temperature", type=float, default=0.0)

    p = sub.add_parser("merge", help="combine partial judged runs (e.g. retries) into one new, immutable file; offline")
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--outputs", type=Path, required=True)
    p.add_argument("--judgements", type=Path, nargs="+", required=True)
    p.add_argument("--output", type=Path, required=True)

    p = sub.add_parser("report", help="human-readable report for one judged run")
    p.add_argument("--outputs", type=Path, required=True)
    p.add_argument("--judgements", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)

    p = sub.add_parser("compare", help="before/after comparison on identical inputs")
    p.add_argument("--before-outputs", type=Path, required=True)
    p.add_argument("--after-outputs", type=Path, required=True)
    p.add_argument("--before-judgements", type=Path)
    p.add_argument("--after-judgements", type=Path)
    p.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            harness = _harness()
            if args.set == "seed":
                data = harness.load_seed()
                cases = [(f"S{i:02d}", t) for i, t in enumerate(data["trigger"], start=1)]
                source = "magicpin-ai-challenge/dataset (seed)"
            else:
                data, pairs = harness.load_expanded()
                cases = [(p["test_id"], p["trigger_id"]) for p in pairs]
                source = "magicpin-ai-challenge/dataset/generate_dataset.py (canonical test_pairs.json)"
            frozen = freeze_cases(data, cases, harness.SEED_NOW, source)
            write_new(args.output, frozen)
            print(f"froze {len(frozen['cases'])} cases -> {args.output} (cases_sha256 {frozen['cases_sha256'][:16]})")
        elif args.command == "run":
            frozen = load_cases(args.cases)
            outputs = run_cases(frozen)
            write_new(args.output, outputs)
            print(f"ran {len(outputs['rows'])} cases from {args.cases} -> {args.output} "
                  f"(emitted {sum(bool(r.get('emitted')) for r in outputs['rows'])})")
        elif args.command == "judge":
            frozen = load_cases(args.cases)
            outputs = json.loads(args.outputs.read_text())
            check_outputs_match(frozen, outputs)
            if args.output.exists():
                raise FileExistsError(f"{args.output} already exists; it is immutable. Choose a new --output path.")
            provider = create_provider(args.provider, args.model, dict(os.environ), args.fixtures, args.temperature)
            print(f"judge: provider={provider.name} model={provider.model} cases={args.only or 'all emitted'}")
            judged = judge_cases(frozen, outputs, provider, only=args.only)
            write_new(args.output, judged)
            statuses = Counter(r["status"] for r in judged["results"])
            print(f"results: {dict(statuses)} -> {args.output}")
            if judged["meta"]["aborted"] or statuses.get("judge_error"):
                print("JUDGE RUN INCOMPLETE — see the errors above")
                return 1
            print("ALL CHECKS PASSED")
        elif args.command == "merge":
            frozen = load_cases(args.cases)
            outputs = json.loads(args.outputs.read_text())
            if args.output.exists():
                raise FileExistsError(f"{args.output} already exists; it is immutable. Choose a new --output path.")
            merged = merge_judgements(frozen, outputs, [(str(p), json.loads(p.read_text())) for p in args.judgements])
            write_new(args.output, merged)
            statuses = Counter(r["status"] for r in merged["results"])
            print(f"merged {len(args.judgements)} artifacts: {dict(statuses)}; superseded failures "
                  f"{[(s['case_id'], s['status']) for s in merged['meta']['superseded']]} -> {args.output}")
        elif args.command == "report":
            text = render_report(json.loads(args.outputs.read_text()), json.loads(args.judgements.read_text()))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n")
            print(f"wrote {args.output}")
        elif args.command == "compare":
            load = (lambda path: json.loads(path.read_text()) if path else None)
            text, changes = render_compare(load(args.before_outputs), load(args.after_outputs),
                                           load(args.before_judgements), load(args.after_judgements))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n")
            print(f"wrote {args.output}")
            if changes:
                print("DECISION FIELDS CHANGED — STOP:\n  " + "\n  ".join(changes))
                return 3
            print("0 decision-field changes")
    except (JudgeConfigError, InputMismatchError, JudgeResponseError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
