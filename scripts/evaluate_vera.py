#!/usr/bin/env python3
"""Developer evaluation: tick outputs for the 30 canonical pairs (or the seed triggers), with mechanical diagnostics.

Drives the real application in-process (``create_app`` + ``/v1/context`` + ``/v1/tick``),
one fresh state per case, at the simulated time the test suite uses. For each case it
records the wire action, re-derives the frozen Phase 2D decision to confirm it matches,
and lists which grounded facts the composer rendered and which it left out.

Metrics are mechanical only (counts, presence checks, the official simulator's
no-LLM fallback formula). Nothing here is a quality score, and nothing here is
imported by the application or the tests::

    .venv/bin/python scripts/evaluate_vera.py                       # 30 canonical pairs
    .venv/bin/python scripts/evaluate_vera.py --set seed            # 25 seed triggers
    .venv/bin/python scripts/evaluate_vera.py --simulator --now 2026-09-28T05:00:00Z
    .venv/bin/python scripts/evaluate_vera.py --json /tmp/before.json
    .venv/bin/python scripts/evaluate_vera.py --compare /tmp/before.json

The expanded dataset is generated into a temporary directory, never into the repository.
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import tempfile
import textwrap
import warnings
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore", message=r".*httpx.*starlette\.testclient")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.engine.candidates import CandidateGenerationContext, generate_candidates  # noqa: E402
from app.engine.composer import CUSTOMER_LABELS, HIDDEN_LABELS, compose, label_of  # noqa: E402
from app.engine.eligibility import evaluate_candidates  # noqa: E402
from app.engine.evidence import is_grounded  # noqa: E402
from app.engine.plans import DecisionPlan  # noqa: E402
from app.engine.planner import load_context  # noqa: E402
from app.engine.selection import select_decision  # noqa: E402
from app.main import create_app  # noqa: E402
from app.state.container import StateContainer  # noqa: E402
from app.state.suppression_store import SuppressionStore  # noqa: E402

DATASET_DIR = ROOT / "magicpin-ai-challenge" / "dataset"
SEED_NOW = datetime(2026, 4, 26, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
"""The simulated decision time of the test suite and the demo."""

SIMULATOR_BATCH = 5
"""``judge_simulator.py`` ``_full``: triggers are ticked five at a time, in file order."""

DIGIT_RUN = re.compile(r"\d+")
"""``judge_simulator.py`` ``_fallback_score``: ``len(re.findall(r'\\d+', body))``."""

LEAKS = {
    "context id": re.compile(r"\b(?:trg|m|c)_\d{3}_\w+"),
    "plan id": re.compile(r"\bplan_[0-9a-f]{6,}"),
    "snake_case code": re.compile(r"\b[a-z]+_[a-z_]+\b"),
    "internal term": re.compile(r"(?i)\b(?:suppress\w*|priority|confidence|archetype|rationale|trigger)\b"),
    "placeholder": re.compile(r"\[uncomposed|\{\{|\{\}|<[a-z_]+>"),
    "url": re.compile(r"https?://"),
}
"""Text that should never reach a recipient (challenge-brief §11; LLMScorer penalty "internal jargon")."""

CTA_SENTENCE = re.compile(r"(?:\?|^Reply CONFIRM\b.*\.)$")

CUSTOMER_TOPIC_WORDS = {
    "recall_due": ("due", "visit"),
    "appointment_tomorrow": ("appointment",),
    "chronic_refill_due": ("refill", "run out"),
    "customer_lapsed_soft": ("last visit",),
    "customer_lapsed_hard": ("last visit",),
    "trial_followup": ("trial",),
    "wedding_package_followup": ("wedding",),
}
"""Words that name each customer trigger's moment; a body with none of them does not say why it was sent."""

CATEGORY_ONLY_WORDS = {"session": {"gyms"}}
"""Customer-facing words that belong to specific categories only."""

HINDI_PREFS = ("hi", "hi-en mix")
HINGLISH_MARKERS = frozenset({"aapka", "aapki", "aapke", "hai", "hain", "ko", "kal", "nahi", "thi", "hongi", "liye", "baar"})
"""Words that only a Hindi-English code-mix body contains (challenge-brief §8 merchant fit: "Is the language preference honored?")."""

RATIONALE_PURPOSE = re.compile(r":\s*(?P<purpose>.+?)\.?\s+priority=")
GENERIC_RATIONALE_WORDS = frozenset({"customer", "customers", "merchant", "their", "which", "about", "before", "instead",
                                      "already", "started", "should", "there", "where", "while"})
"""Rationale words that name roles or grammar rather than the message's topic."""


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


def load_seed() -> dict[str, dict[str, dict[str, Any]]]:
    categories = [json.loads(p.read_text()) for p in sorted((DATASET_DIR / "categories").glob("*.json"))]
    return {
        "category": {c["slug"]: c for c in categories},
        "merchant": {m["merchant_id"]: m for m in json.loads((DATASET_DIR / "merchants_seed.json").read_text())["merchants"]},
        "customer": {c["customer_id"]: c for c in json.loads((DATASET_DIR / "customers_seed.json").read_text())["customers"]},
        "trigger": {t["id"]: t for t in json.loads((DATASET_DIR / "triggers_seed.json").read_text())["triggers"]},
    }


def load_expanded() -> tuple[dict[str, dict[str, dict[str, Any]]], list[dict[str, Any]]]:
    """Run the official generator into a temp dir; return the contexts and ``test_pairs.json``."""
    with tempfile.TemporaryDirectory(prefix="vera_eval_") as tmp:
        out = Path(tmp) / "expanded"
        subprocess.run(
            [sys.executable, str(DATASET_DIR / "generate_dataset.py"), "--seed-dir", str(DATASET_DIR), "--out", str(out)],
            cwd=tmp, check=True, capture_output=True,
        )

        def load(sub: str, key: str) -> dict[str, dict[str, Any]]:
            return {d[key]: d for d in (json.loads(p.read_text()) for p in sorted((out / sub).glob("*.json")))}

        data = {
            "category": load("categories", "slug"),
            "merchant": load("merchants", "merchant_id"),
            "customer": load("customers", "customer_id"),
            "trigger": load("triggers", "id"),
        }
        pairs = json.loads((out / "test_pairs.json").read_text())["pairs"]
    return data, pairs


def contexts_for(data: dict[str, dict[str, dict[str, Any]]], trigger_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    trigger = data["trigger"][trigger_id]
    merchant = data["merchant"][trigger["merchant_id"]]
    out = [("category", merchant["category_slug"], data["category"][merchant["category_slug"]]),
           ("merchant", merchant["merchant_id"], merchant)]
    if trigger.get("customer_id") in data["customer"]:
        out.append(("customer", trigger["customer_id"], data["customer"][trigger["customer_id"]]))
    out.append(("trigger", trigger_id, trigger))
    return out


# --------------------------------------------------------------------------- #
# Running the application
# --------------------------------------------------------------------------- #


class App:
    """A fresh in-process Vera: the real routers over a new StateContainer."""

    def __init__(self) -> None:
        self.state = StateContainer.create()
        self.client = TestClient(create_app(settings=Settings(), state=self.state))
        logging.disable(logging.INFO)

    def push(self, scope: str, context_id: str, payload: dict[str, Any], now: datetime) -> None:
        response = self.client.post("/v1/context", json={
            "scope": scope, "context_id": context_id, "version": 1, "payload": payload, "delivered_at": now.isoformat(),
        })
        if response.status_code != 200 or not response.json().get("accepted"):
            raise SystemExit(f"context push rejected: {scope}/{context_id}: {response.status_code} {response.text[:200]}")

    def tick(self, trigger_ids: list[str], now: datetime) -> list[dict[str, Any]]:
        response = self.client.post("/v1/tick", json={"now": now.isoformat(), "available_triggers": trigger_ids})
        if response.status_code != 200:
            raise SystemExit(f"tick failed: {response.status_code} {response.text[:200]}")
        return response.json()["actions"]


def frozen_decision(app: App, trigger_id: str, now: datetime) -> tuple[DecisionPlan | None, CandidateGenerationContext | None, str]:
    """The Phase 2D decision for one trigger, derived exactly as the canonical tests do (no prior suppression)."""
    ctx = load_context(app.state.context_store, trigger_id, now)
    if not isinstance(ctx, CandidateGenerationContext):
        return None, None, ctx.outcome.value
    plan = select_decision(ctx, evaluate_candidates(generate_candidates(ctx), ctx, SuppressionStore(), now=now))
    return plan, ctx, "no_action" if plan.is_no_action else "emitted"


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?\u201d])\s+(?=[A-Z\u201c])", text.strip())
    return [p for p in parts if p]


def context_numbers(*payloads: Any) -> set[str]:
    """Digit runs that appear in the contexts, including percent and humanized-date forms of their values."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, bool):
            return
        elif isinstance(value, int | float):
            found.update(DIGIT_RUN.findall(str(value)))
            found.update(DIGIT_RUN.findall(str(round(abs(value) * 100, 1)).removesuffix(".0")))
            found.add(str(int(abs(value))))
        elif isinstance(value, str):
            found.update(DIGIT_RUN.findall(value))
            found.update(str(int(run)) for run in DIGIT_RUN.findall(value))
            if re.match(r"\d{4}-\d{2}-\d{2}T(\d{2})", value):
                hour = int(value[11:13])
                found.add(str(hour % 12 or 12))

    for payload in payloads:
        walk(payload)
    return found


def diagnose(action: dict[str, Any], plan: DecisionPlan, ctx: CandidateGenerationContext) -> dict[str, Any]:
    body = action["body"]
    message = compose(plan, ctx)
    parts = sentences(body)
    runs = DIGIT_RUN.findall(body.lower())
    known = context_numbers(ctx.category, ctx.merchant, ctx.trigger, ctx.customer)
    customer = plan.scope.value == "customer"

    grounded = [e for e in sorted(plan.evidence, key=lambda e: -e.importance)
                if is_grounded(e, ctx.source_data(e.source)) and label_of(e) not in HIDDEN_LABELS]
    rendered = set(message.facts_used)
    unused = [f"{label_of(e)} ({e.source}:{e.field})" for e in grounded
              if f"{e.source}:{e.field}" not in rendered and (not customer or label_of(e) in CUSTOMER_LABELS)]

    identity = (ctx.merchant.get("identity") or {})
    person = ((ctx.customer or {}).get("identity") or {}).get("name") if customer else identity.get("owner_first_name")
    if isinstance(person, str):
        parent = re.search(r"\(parent:\s*(.+?)\)", person)
        person = parent.group(1) if parent else re.sub(r"\s*\(.*", "", person)
    else:
        person = None
    merchant_numbers = [e for e in grounded if e.source.value == "merchant"
                        and isinstance(e.value, int | float) and not isinstance(e.value, bool)]
    kind = ctx.canonical_kind
    lowered = body.lower()
    digest_sources = [d.get("source") for d in ctx.category.get("digest") or [] if isinstance(d, dict) and d.get("source")]
    last = parts[-1] if parts else ""
    voice = ctx.category.get("voice") or {}
    words = set(re.findall(r"[a-z]+", lowered))
    language_pref = ((ctx.customer or {}).get("identity") or {}).get("language_pref")
    purpose = RATIONALE_PURPOSE.search(action["rationale"] or "")
    purpose_words = {w.removesuffix("'s") for w in re.findall(r"[a-z']{5,}", purpose.group("purpose").lower())} if purpose else set()

    return {
        "chars": len(body),
        "sentences": len(parts),
        "questions": body.count("?"),
        "cta_in_last_sentence": bool(CTA_SENTENCE.search(last)) if action["cta"] != "none" else "?" not in body,
        "digit_runs": len(runs),
        "simulator_fallback_specificity": min(10, 3 + len(runs) * 2),
        "numbers_not_found_in_contexts": sorted({r for r in runs if r not in known and str(int(r)) not in known}),
        "facts_rendered": len(message.facts_used),
        "trigger_facts_rendered": sum(f.startswith("trigger:") for f in message.facts_used),
        "grounded_evidence": len(grounded),
        "grounded_not_rendered": unused,
        "merchant_numbers_grounded": len(merchant_numbers),
        "merchant_numbers_rendered": sum(f"{e.source}:{e.field}" in rendered for e in merchant_numbers),
        "names_trigger_moment": (any(w in lowered for w in CUSTOMER_TOPIC_WORDS[kind]) if customer and kind in CUSTOMER_TOPIC_WORDS
                                 else None),
        "off_category_words": sorted(w for w, cats in CATEGORY_ONLY_WORDS.items()
                                     if customer and re.search(rf"\b{w}\b", lowered) and ctx.category.get("slug") not in cats),
        "names_person": bool(person) and person in body,
        "business_named": bool(identity.get("name")) and identity["name"] in body,
        "cites_source": "Source:" in body or any(s in body for s in digest_sources),
        "cta_count": body.count("?") + body.count("Reply CONFIRM"),
        "category_vocab_used": sorted(t for t in voice.get("vocab_allowed") or [] if t.lower() in lowered),
        "category_taboo_used": sorted(t for t in voice.get("vocab_taboo") or [] if t.lower() in lowered),
        "merchant_languages": identity.get("languages"),
        "customer_language_pref": language_pref,
        "hinglish_markers": sorted(words & HINGLISH_MARKERS),
        "language_match": (bool(words & HINGLISH_MARKERS) == (language_pref in HINDI_PREFS)) if customer else None,
        "rationale_topic_missing": sorted(w for w in purpose_words - GENERIC_RATIONALE_WORDS
                                          if not any(token.startswith(w[:5]) for token in words)),
        "suppression_key_in_body": bool(action["suppression_key"]) and action["suppression_key"] in body,
        "leaks": sorted({name for name, pattern in LEAKS.items() if pattern.search(body)}),
        "wire_matches_composer": (body, action["template_name"], action["template_params"]) == (
            message.body, message.template_name, list(message.template_params)),
        "facts_used": list(message.facts_used),
    }


def record(case_id: str, trigger: dict[str, Any], merchant: dict[str, Any], action: dict[str, Any] | None,
           plan: DecisionPlan | None, ctx: CandidateGenerationContext | None, outcome: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "case_id": case_id, "trigger_id": trigger["id"], "trigger_kind": trigger.get("kind"),
        "category": merchant.get("category_slug"), "merchant_id": trigger.get("merchant_id"),
        "customer_id": trigger.get("customer_id"), "outcome": outcome,
    }
    if plan is not None:
        row |= {"archetype": plan.archetype.value, "action": plan.action.value, "scope": plan.scope.value,
                "selected_offer_id": plan.selected_offer_id, "plan_id": plan.plan_id,
                "plan_suppression_key": plan.suppression_key}
    if action is None:
        row["emitted"] = False
        return row
    row |= {
        "emitted": True, "send_as": action["send_as"], "cta": action["cta"], "template_name": action["template_name"],
        "suppression_key": action["suppression_key"], "rationale": action["rationale"], "body": action["body"],
    }
    if plan is not None and ctx is not None:
        row["decision_matches_phase_2d"] = (
            action["suppression_key"] == plan.suppression_key and action["send_as"] == plan.send_as.value
            and action["customer_id"] == plan.customer_id and f"plan_id={plan.plan_id}" in action["rationale"]
        )
        row["metrics"] = diagnose(action, plan, ctx)
    return row


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #


def evaluate_cases(data: dict[str, dict[str, dict[str, Any]]], cases: list[tuple[str, str]], now: datetime) -> list[dict[str, Any]]:
    """One fresh application per case, as the canonical tests do."""
    rows = []
    for case_id, trigger_id in cases:
        app = App()
        for scope, context_id, payload in contexts_for(data, trigger_id):
            app.push(scope, context_id, payload, now)
        plan, ctx, outcome = frozen_decision(app, trigger_id, now)
        actions = app.tick([trigger_id], now)
        trigger = data["trigger"][trigger_id]
        rows.append(record(case_id, trigger, data["merchant"][trigger["merchant_id"]], actions[0] if actions else None, plan, ctx, outcome))
    return rows


def evaluate_simulator(data: dict[str, dict[str, dict[str, Any]]], now: datetime) -> list[dict[str, Any]]:
    """``judge_simulator.py`` ``_full`` inputs: categories, all merchants and triggers (no customers), batches of 5."""
    app = App()
    for scope in ("category", "merchant", "trigger"):
        for context_id, payload in data[scope].items():
            app.push(scope, context_id, payload, now)
    rows = []
    trigger_ids = list(data["trigger"])
    for start in range(0, len(trigger_ids), SIMULATOR_BATCH):
        batch = trigger_ids[start:start + SIMULATOR_BATCH]
        decisions = {t: frozen_decision(app, t, now) for t in batch}
        emitted = {a["trigger_id"]: a for a in app.tick(batch, now)}
        for trigger_id in batch:
            plan, ctx, outcome = decisions[trigger_id]
            trigger = data["trigger"][trigger_id]
            rows.append(record(f"B{start // SIMULATOR_BATCH + 1}", trigger, data["merchant"].get(trigger["merchant_id"], {}),
                               emitted.get(trigger_id), plan, ctx, outcome))
    return rows


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def print_rows(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        head = f"{row['case_id']}  {row['trigger_id']}  [{row.get('trigger_kind')} / {row.get('category')}]"
        print("\n" + head)
        if not row.get("emitted"):
            print(f"    no send: outcome={row['outcome']} action={row.get('action', '-')}")
            continue
        m = row["metrics"]
        print(f"    action={row['action']} scope={row['scope']} send_as={row['send_as']} cta={row['cta']} "
              f"offer={row['selected_offer_id']} template={row['template_name']}")
        print(textwrap.indent(textwrap.fill(row["body"], 96), "    | "))
        print(f"    chars={m['chars']} sentences={m['sentences']} questions={m['questions']} "
              f"cta_last={m['cta_in_last_sentence']} digit_runs={m['digit_runs']} "
              f"(simulator fallback specificity {m['simulator_fallback_specificity']}/10)")
        print(f"    facts rendered={m['facts_rendered']} (trigger {m['trigger_facts_rendered']}) of grounded evidence="
              f"{m['grounded_evidence']}; names person={m['names_person']} cites source={m['cites_source']}")
        if m["grounded_not_rendered"]:
            print(f"    grounded but not rendered: {'; '.join(m['grounded_not_rendered'])}")
        flags = [f"leaks={m['leaks']}" if m["leaks"] else "",
                 "does not name its trigger's moment" if m["names_trigger_moment"] is False else "",
                 f"off-category words={m['off_category_words']}" if m["off_category_words"] else "",
                 "merchant's own figure not rendered" if m["merchant_numbers_grounded"] and not m["merchant_numbers_rendered"] else "",
                 f"numbers not found in contexts={m['numbers_not_found_in_contexts']}" if m["numbers_not_found_in_contexts"] else "",
                 "" if row["decision_matches_phase_2d"] else "DECISION MISMATCH vs Phase 2D",
                 "" if m["wire_matches_composer"] else "WIRE != COMPOSER"]
        if any(flags):
            print("    " + "  ".join(f for f in flags if f))


def print_summary(rows: list[dict[str, Any]]) -> None:
    sent = [r for r in rows if r.get("emitted")]
    metrics = [r["metrics"] for r in sent]
    print("\n" + "=" * 100)
    print(f"cases={len(rows)} emitted={len(sent)} not emitted={len(rows) - len(sent)} "
          f"outcomes={dict(Counter(r['outcome'] for r in rows))}")
    if not sent:
        return
    print(f"decision matches Phase 2D: {sum(r['decision_matches_phase_2d'] for r in sent)}/{len(sent)}; "
          f"wire == composer: {sum(m['wire_matches_composer'] for m in metrics)}/{len(sent)}")
    print(f"CTA types: {dict(Counter(r['cta'] for r in sent))}; send_as: {dict(Counter(r['send_as'] for r in sent))}")
    print(f"CTA in last sentence: {sum(m['cta_in_last_sentence'] for m in metrics)}/{len(sent)}; "
          f"questions per body: {dict(Counter(m['questions'] for m in metrics))}")
    print(f"chars: min={min(m['chars'] for m in metrics)} max={max(m['chars'] for m in metrics)}; "
          f"digit runs per body: {dict(sorted(Counter(m['digit_runs'] for m in metrics).items()))}")
    print(f"simulator fallback specificity: {dict(sorted(Counter(m['simulator_fallback_specificity'] for m in metrics).items()))}")
    print(f"names the person: {sum(m['names_person'] for m in metrics)}/{len(sent)}; "
          f"with leaks: {sum(bool(m['leaks']) for m in metrics)}; "
          f"with numbers not found in contexts: {sum(bool(m['numbers_not_found_in_contexts']) for m in metrics)}")
    with_numbers = [m for m in metrics if m["merchant_numbers_grounded"]]
    print(f"merchant's own figures: rendered in {sum(bool(m['merchant_numbers_rendered']) for m in with_numbers)}"
          f"/{len(with_numbers)} bodies whose plan grounds one")
    moments = [m for m in metrics if m["names_trigger_moment"] is not None]
    print(f"customer bodies naming their trigger's moment: {sum(m['names_trigger_moment'] for m in moments)}/{len(moments)}; "
          f"with off-category words: {sum(bool(m['off_category_words']) for m in metrics)}")
    print(f"CTA count per body: {dict(Counter(m['cta_count'] for m in metrics))}; business named: "
          f"{sum(m['business_named'] for m in metrics)}/{len(sent)}; suppression key in body: "
          f"{sum(m['suppression_key_in_body'] for m in metrics)}; category taboo used: {sum(bool(m['category_taboo_used']) for m in metrics)}")
    languages = [m for m in metrics if m["language_match"] is not None]
    print(f"customer bodies matching language preference: {sum(m['language_match'] for m in languages)}/{len(languages)}; "
          f"bodies with rationale topic words missing: {sum(bool(m['rationale_topic_missing']) for m in metrics)}/{len(sent)}")
    print(f"with grounded evidence not rendered: {sum(bool(m['grounded_not_rendered']) for m in metrics)}/{len(sent)}")
    omitted = Counter(label.split(" (")[0] for m in metrics for label in m["grounded_not_rendered"])
    if omitted:
        print(f"  most often not rendered: {dict(omitted.most_common(12))}")


def print_compare(rows: list[dict[str, Any]], before_path: Path) -> None:
    before = {r["case_id"] + r["trigger_id"]: r for r in json.loads(before_path.read_text())}
    decision_fields = ("outcome", "action", "scope", "selected_offer_id", "plan_id", "suppression_key", "send_as", "cta", "rationale")
    changed = 0
    print("\n" + "=" * 100 + f"\nA/B against {before_path}")
    for row in rows:
        old = before.get(row["case_id"] + row["trigger_id"])
        if old is None:
            print(f"  {row['case_id']} {row['trigger_id']}: not in the before run")
            continue
        moved = [f for f in decision_fields if old.get(f) != row.get(f)]
        if moved:
            print(f"  DECISION FIELD CHANGED {row['case_id']} {row['trigger_id']}: {moved}")
        if old.get("body") != row.get("body"):
            changed += 1
            om, nm = old.get("metrics", {}), row.get("metrics", {})
            print(f"\n  {row['case_id']} {row['trigger_id']}  digit runs {om.get('digit_runs')} -> {nm.get('digit_runs')}, "
                  f"facts {om.get('facts_rendered')} -> {nm.get('facts_rendered')}, chars {om.get('chars')} -> {nm.get('chars')}")
            print(textwrap.indent(textwrap.fill(old.get("body") or "-", 96), "    before | "))
            print(textwrap.indent(textwrap.fill(row.get("body") or "-", 96), "    after  | "))
    print(f"\nbodies changed: {changed}/{len(rows)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--set", choices=("canonical", "seed"), default="canonical", help="case set (default: canonical)")
    parser.add_argument("--simulator", action="store_true", help="reproduce judge_simulator.py full_evaluation inputs (seed data)")
    parser.add_argument("--now", help="ISO tick time (default: the suite's simulated time, 2026-04-26T10:00+05:30)")
    parser.add_argument("--json", type=Path, help="write the records to this file")
    parser.add_argument("--compare", type=Path, help="A/B: diff bodies and decision fields against an earlier --json file")
    parser.add_argument("--quiet", action="store_true", help="summary only")
    args = parser.parse_args()

    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else SEED_NOW
    if args.simulator:
        rows = evaluate_simulator(load_seed(), now)
    elif args.set == "seed":
        seed = load_seed()
        rows = evaluate_cases(seed, [(f"S{i:02d}", t) for i, t in enumerate(seed["trigger"], start=1)], now)
    else:
        data, pairs = load_expanded()
        rows = evaluate_cases(data, [(p["test_id"], p["trigger_id"]) for p in pairs], now)

    print(f"Vera evaluation — set={'simulator' if args.simulator else args.set} now={now.isoformat()}")
    if not args.quiet:
        print_rows(rows)
    print_summary(rows)
    if args.compare:
        print_compare(rows, args.compare)
    if args.json:
        args.json.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
