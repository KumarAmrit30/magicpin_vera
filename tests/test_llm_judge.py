"""scripts/llm_judge.py: frozen inputs, strict schema, providers, reports. No network is used."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib import error as urlerror

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm_judge" / "responses.json"
SECRET = "test-key-9f3a-SECRET"

_spec = importlib.util.spec_from_file_location("llm_judge", ROOT / "scripts" / "llm_judge.py")
lj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lj)

CASES = [("S03", "trg_003_recall_due_priya"), ("S10", "trg_010_ipl_match_delhi")]


@pytest.fixture(scope="module")
def frozen() -> dict[str, Any]:
    harness = lj._harness()
    return lj.freeze_cases(harness.load_seed(), CASES, harness.SEED_NOW, "seed (test)")


@pytest.fixture(scope="module")
def outputs(frozen: dict[str, Any]) -> dict[str, Any]:
    try:
        return lj.run_cases(frozen)
    finally:
        logging.disable(logging.NOTSET)


def fixture_judgement(case_id: str = "S03") -> dict[str, Any]:
    return copy.deepcopy(json.loads(FIXTURES.read_text())["responses"][case_id])


# --------------------------------------------------------------------------- #
# Frozen inputs: before and after use identical cases
# --------------------------------------------------------------------------- #


def test_frozen_file_round_trips_and_detects_edits(frozen: dict[str, Any], tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    lj.write_new(path, frozen)
    assert lj.load_cases(path)["cases_sha256"] == frozen["cases_sha256"]

    with pytest.raises(FileExistsError):
        lj.write_new(path, frozen)

    edited = json.loads(path.read_text())
    edited["cases"][0]["contexts"][1]["payload"]["identity"]["name"] = "Someone Else"
    path.write_text(json.dumps(edited))
    with pytest.raises(lj.InputMismatchError, match="S03"):
        lj.load_cases(path)


def test_before_and_after_runs_use_identical_case_inputs(frozen: dict[str, Any], outputs: dict[str, Any]) -> None:
    again = lj.run_cases(frozen)
    logging.disable(logging.NOTSET)

    assert again["cases_sha256"] == outputs["cases_sha256"] == frozen["cases_sha256"]
    assert [r["input_sha256"] for r in again["rows"]] == [c["input_sha256"] for c in frozen["cases"]]
    text, changes = lj.render_compare(outputs, again, None, None)
    assert changes == []
    assert "identical inputs" in text and "0 decision-field changes" in text


def test_outputs_from_other_inputs_are_refused(frozen: dict[str, Any], outputs: dict[str, Any]) -> None:
    harness = lj._harness()
    other = lj.freeze_cases(harness.load_seed(), CASES, harness.SEED_NOW + timedelta(days=1), "seed (other day)")
    assert other["cases_sha256"] != frozen["cases_sha256"]

    with pytest.raises(lj.InputMismatchError):
        lj.check_outputs_match(other, outputs)
    tampered = copy.deepcopy(outputs)
    tampered["rows"][0]["input_sha256"] = "0" * 64
    with pytest.raises(lj.InputMismatchError):
        lj.check_outputs_match(frozen, tampered)
    moved = copy.deepcopy(outputs)
    moved["cases_sha256"] = other["cases_sha256"]
    with pytest.raises(lj.InputMismatchError):
        lj.render_compare(outputs, moved, None, None)


def test_compare_reports_decision_changes(outputs: dict[str, Any]) -> None:
    after = copy.deepcopy(outputs)
    after["rows"][0]["plan_id"] = "plan_changed"
    after["rows"][1]["body"] = after["rows"][1]["body"] + " Extra."

    text, changes = lj.render_compare(outputs, after, None, None)

    assert changes == [f"S03: plan_id {outputs['rows'][0]['plan_id']!r} -> 'plan_changed'"]
    assert "DECISION FIELD CHANGED" in text
    assert "Bodies changed: 1 (S10)" in text


# --------------------------------------------------------------------------- #
# Judge input
# --------------------------------------------------------------------------- #


def test_judge_input_is_the_message_as_sent_without_internals(frozen: dict[str, Any], outputs: dict[str, Any]) -> None:
    case, row = frozen["cases"][0], outputs["rows"][0]
    judge_input = lj.build_judge_input(case, row)

    assert set(judge_input) == {"case_id", "category", "merchant", "customer", "trigger", "vera_output", "facts_used"}
    assert judge_input["vera_output"] == {f: row[f] for f in ("action", "scope", "cta", "send_as", "body", "rationale")}
    assert judge_input["customer"]["identity"]["name"] == "Priya"
    assert {"ref": "merchant:identity.name", "value": "Dr. Meera's Dental Clinic"} in judge_input["facts_used"]
    flat = json.dumps(judge_input)
    for internal in ("candidates", "metrics", "archetype", "selected_offer_id", "plan_suppression_key", "decision_matches"):
        assert f'"{internal}"' not in flat


def test_resolve_ref_walks_lists_and_misses_cleanly() -> None:
    contexts = {"merchant": {"offers": [{"title": "A"}]}, "trigger": {"kind": "recall_due"}}
    assert lj.resolve_ref("merchant:offers.0.title", contexts) == "A"
    assert lj.resolve_ref("trigger:kind", contexts) == "recall_due"
    assert lj.resolve_ref("merchant:offers.5.title", contexts) is None
    assert lj.resolve_ref("customer:identity.name", contexts) is None


# --------------------------------------------------------------------------- #
# Strict schema
# --------------------------------------------------------------------------- #


def test_valid_judgement_passes_every_check() -> None:
    checks = lj.schema_checks(json.dumps(fixture_judgement()), "S03")
    assert [name for name, _, _ in checks] == [
        "valid JSON", "strict top-level schema", "five dimensions, integer scores 0-10 with reasons", "groundedness object",
        "rationale_alignment object", "cta_assessment object", "critical_issues / suggested_improvements lists"]
    assert all(passed for _, passed, _ in checks)
    assert lj.validate_judgement(json.dumps(fixture_judgement()), "S03")["dimensions"]["specificity"]["score"] == 8


@pytest.mark.parametrize("mutate, message", [
    (lambda j: j["dimensions"].pop("merchant_fit"), "missing=['merchant_fit']"),
    (lambda j: j["dimensions"]["specificity"].update(score=11), "integer 0-10"),
    (lambda j: j["dimensions"]["specificity"].update(score="7"), "integer 0-10"),
    (lambda j: j["dimensions"]["specificity"].update(score=True), "integer 0-10"),
    (lambda j: j["dimensions"]["specificity"].update(score=7.5), "integer 0-10"),
    (lambda j: j["dimensions"]["category_fit"].update(reason=None), "reason must be a string"),
    (lambda j: j.update(overall=40), "unexpected=['overall']"),
    (lambda j: j.update(case_id="S10"), "does not echo"),
    (lambda j: j["groundedness"].pop("missing_context_used"), "groundedness"),
    (lambda j: j["groundedness"].update(unsupported_claims="none"), "list of strings"),
    (lambda j: j["rationale_alignment"].update(issues=[{"issue": "x"}]), "list of strings"),
    (lambda j: j["cta_assessment"].update(score=-1), "integer 0-10"),
    (lambda j: j.pop("critical_issues"), "missing=['critical_issues']"),
])
def test_schema_violations_are_rejected(mutate: Any, message: str) -> None:
    judgement = fixture_judgement()
    mutate(judgement)
    with pytest.raises(lj.JudgeResponseError, match=message.replace("[", r"\[").replace("]", r"\]")):
        lj.validate_judgement(json.dumps(judgement), "S03")


def test_non_json_is_rejected() -> None:
    assert lj.schema_checks("```json\n{}\n```", "S03")[0][:2] == ("valid JSON", False)


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


def test_missing_key_fails_clearly_and_never_falls_back() -> None:
    with pytest.raises(lj.JudgeConfigError, match="VERA_EVAL_API_KEY is not set"):
        lj.create_provider("gemini", "gemini-3.5-flash", env={})
    with pytest.raises(lj.JudgeConfigError, match="VERA_EVAL_API_KEY is not set"):
        lj.create_provider(None, "gemini-3.5-flash", env={"VERA_EVAL_PROVIDER": "gemini", "VERA_EVAL_API_KEY": "  "})
    with pytest.raises(lj.JudgeConfigError, match="unknown provider"):
        lj.create_provider("openai", "x", env={"VERA_EVAL_API_KEY": SECRET})
    with pytest.raises(lj.JudgeConfigError, match="unknown provider"):
        lj.create_provider(None, None, env={})
    with pytest.raises(lj.JudgeConfigError, match="invalid model name"):
        lj.create_provider("gemini", "../models/x?key=1", env={"VERA_EVAL_API_KEY": SECRET})


def test_env_configuration_selects_gemini() -> None:
    provider = lj.create_provider(None, None, env={"VERA_EVAL_PROVIDER": "gemini", "VERA_EVAL_MODEL": "gemini-3.5-flash",
                                                   "VERA_EVAL_API_KEY": SECRET})
    assert (provider.name, provider.model, provider.temperature) == ("gemini", "gemini-3.5-flash", 0.0)


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def gemini_opener(text_parts: list[dict[str, Any]], seen: list[Any]) -> Any:
    def opener(request: Any, timeout: float) -> FakeResponse:
        seen.append(request)
        return FakeResponse(json.dumps({"candidates": [{"content": {"parts": text_parts}, "finishReason": "STOP"}]}).encode())
    return opener


def test_gemini_request_keeps_the_key_in_the_header_only() -> None:
    seen: list[Any] = []
    provider = lj.GeminiProvider(SECRET, "gemini-3.5-flash",
                                 opener=gemini_opener([{"text": "ignored", "thought": True}, {"text": '{"a": 1}'}], seen))

    assert provider.complete("SYSTEM", "PROMPT", "S03") == '{"a": 1}'

    request = seen[0]
    assert request.full_url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    assert request.get_header("X-goog-api-key") == SECRET
    body = json.loads(request.data)
    assert SECRET not in request.full_url and SECRET not in request.data.decode()
    assert body["generationConfig"] == {"temperature": 0.0, "responseMimeType": "application/json"}
    assert body["systemInstruction"]["parts"][0]["text"] == "SYSTEM"


def test_gemini_errors_are_redacted() -> None:
    def failing(request: Any, timeout: float) -> Any:
        raise urlerror.HTTPError(request.full_url, 403, "Forbidden", {},
                                 io.BytesIO(json.dumps({"error": {"message": f"API key {SECRET} not valid"}}).encode()))

    with pytest.raises(lj.ProviderError) as info:
        lj.GeminiProvider(SECRET, "gemini-3.5-flash", opener=failing).complete("s", "p", "S03")
    assert str(info.value) == "HTTP 403: API key [REDACTED] not valid"


def test_fixture_provider_round_trip_and_missing_case() -> None:
    provider = lj.FixtureProvider(FIXTURES)
    assert json.loads(provider.complete("s", "p", "S10"))["case_id"] == "S10"
    with pytest.raises(lj.ProviderError, match="no stored response for case S99"):
        provider.complete("s", "p", "S99")


# --------------------------------------------------------------------------- #
# Judging runs
# --------------------------------------------------------------------------- #


def test_fixture_judge_run_records_every_case(frozen: dict[str, Any], outputs: dict[str, Any]) -> None:
    judged = lj.judge_cases(frozen, outputs, lj.FixtureProvider(FIXTURES), log=lambda _: None)

    assert [(r["case_id"], r["status"]) for r in judged["results"]] == [("S03", "ok"), ("S10", "ok")]
    assert judged["cases_sha256"] == frozen["cases_sha256"]
    assert judged["outputs_sha256"] == lj.sha256_json(outputs["rows"])
    assert judged["meta"]["provider"] == "fixture" and judged["meta"]["system_prompt_sha256"] == lj.sha256_text(lj.SYSTEM_PROMPT)


def test_only_restricts_the_run_and_rejects_unknown_ids(frozen: dict[str, Any], outputs: dict[str, Any]) -> None:
    judged = lj.judge_cases(frozen, outputs, lj.FixtureProvider(FIXTURES), only=["S10"], log=lambda _: None)
    assert [r["case_id"] for r in judged["results"]] == ["S10"]
    with pytest.raises(lj.InputMismatchError, match="T99"):
        lj.judge_cases(frozen, outputs, lj.FixtureProvider(FIXTURES), only=["T99"], log=lambda _: None)


def test_invalid_responses_become_errors_never_scores(frozen: dict[str, Any], outputs: dict[str, Any], tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    broken = fixture_judgement("S10")
    broken["dimensions"]["specificity"]["score"] = 12
    bad.write_text(json.dumps({"responses": {"S03": "not json at all", "S10": broken}}))
    lines: list[str] = []

    judged = lj.judge_cases(frozen, outputs, lj.FixtureProvider(bad), log=lines.append)

    assert [r["status"] for r in judged["results"]] == ["judge_error", "judge_error"]
    assert all("judgement" not in r and len(r["errors"]) == 2 for r in judged["results"])
    assert "[FAIL] S03 valid JSON" in "\n".join(lines)


def test_provider_failure_stops_the_run(frozen: dict[str, Any], outputs: dict[str, Any]) -> None:
    def failing(request: Any, timeout: float) -> Any:
        raise urlerror.URLError("connection refused")

    judged = lj.judge_cases(frozen, outputs, lj.GeminiProvider(SECRET, "gemini-3.5-flash", opener=failing), log=lambda _: None)

    assert [(r["case_id"], r["status"]) for r in judged["results"]] == [("S03", "provider_error")]
    assert judged["meta"]["aborted"].startswith("S03: request failed")


def test_no_saved_file_contains_the_key(frozen: dict[str, Any], outputs: dict[str, Any], tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    responses = json.loads(FIXTURES.read_text())["responses"]
    served = iter([[{"text": json.dumps(responses["S03"])}], [{"text": json.dumps(responses["S10"])}]])

    def opener(request: Any, timeout: float) -> FakeResponse:
        return gemini_opener(next(served), [])(request, timeout)

    monkeypatch.setattr(lj.time, "sleep", lambda _: None)
    provider = lj.GeminiProvider(SECRET, "gemini-3.5-flash", opener=opener)
    judged = lj.judge_cases(frozen, outputs, provider, log=lambda _: None)
    lj.write_new(tmp_path / "judgements.json", judged)
    (tmp_path / "report.md").write_text(lj.render_report(outputs, judged))

    assert [r["status"] for r in judged["results"]] == ["ok", "ok"]
    for path in tmp_path.iterdir():
        assert SECRET not in path.read_text(), path.name


def test_cli_judge_without_key_exits_clearly(frozen: dict[str, Any], outputs: dict[str, Any], tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("VERA_EVAL_API_KEY", raising=False)
    lj.write_new(tmp_path / "cases.json", frozen)
    lj.write_new(tmp_path / "outputs.json", outputs)

    code = lj.main(["judge", "--cases", str(tmp_path / "cases.json"), "--outputs", str(tmp_path / "outputs.json"),
                    "--provider", "gemini", "--model", "gemini-3.5-flash", "--output", str(tmp_path / "j.json")])

    assert code == 2
    assert "VERA_EVAL_API_KEY is not set" in capsys.readouterr().err
    assert not (tmp_path / "j.json").exists()


# --------------------------------------------------------------------------- #
# Merging partial runs (retries)
# --------------------------------------------------------------------------- #


def partial_runs(frozen: dict[str, Any], outputs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """An aborted full run (S03 ok, S10 timed out) and a retry of S10."""
    first = lj.judge_cases(frozen, outputs, lj.FixtureProvider(FIXTURES), only=["S03"], log=lambda _: None)
    first["results"].append({"case_id": "S10", "input_sha256": outputs["rows"][1]["input_sha256"],
                             "status": "provider_error", "error": "request failed: The read operation timed out"})
    retry = lj.judge_cases(frozen, outputs, lj.FixtureProvider(FIXTURES), only=["S10"], log=lambda _: None)
    return first, retry


def test_merge_keeps_judgements_verbatim_and_records_superseded_failures(frozen: dict[str, Any], outputs: dict[str, Any]) -> None:
    first, retry = partial_runs(frozen, outputs)

    merged = lj.merge_judgements(frozen, outputs, [("full.json", first), ("retry.json", retry)])

    assert [(r["case_id"], r["status"]) for r in merged["results"]] == [("S03", "ok"), ("S10", "ok")]
    assert merged["results"][0] == first["results"][0] and merged["results"][1] == retry["results"][0]
    assert merged["meta"]["superseded"] == [{"case_id": "S10", "status": "provider_error", "from": "full.json",
                                             "error": "request failed: The read operation timed out"}]
    assert merged["meta"]["merged_from"] == [{"artifact": "full.json", "judged_cases": ["S03"]},
                                             {"artifact": "retry.json", "judged_cases": ["S10"]}]
    assert (merged["cases_sha256"], merged["outputs_sha256"]) == (first["cases_sha256"], first["outputs_sha256"])
    assert "Merged from:" in lj.render_report(outputs, merged)


def test_merge_preserves_not_emitted_cases(frozen: dict[str, Any], outputs: dict[str, Any]) -> None:
    quiet = copy.deepcopy(outputs)
    quiet["rows"][1] = {k: quiet["rows"][1][k] for k in ("case_id", "trigger_id", "input_sha256", "outcome")} | {
        "emitted": False, "outcome": "no_action"}
    judged = lj.judge_cases(frozen, quiet, lj.FixtureProvider(FIXTURES), log=lambda _: None)

    merged = lj.merge_judgements(frozen, quiet, [("run.json", judged)])

    assert merged["results"][1] == {"case_id": "S10", "input_sha256": quiet["rows"][1]["input_sha256"],
                                    "status": "not_emitted", "outcome": "no_action"}


@pytest.mark.parametrize("break_it, message", [
    (lambda first, retry: retry["results"].append(copy.deepcopy(first["results"][0])), "S03 has a judgement in both"),
    (lambda first, retry: retry["results"].clear(), r"without a valid judgement: \['S10'\]"),
    (lambda first, retry: retry["results"][0]["judgement"]["dimensions"]["specificity"].update(score=11), "integer 0-10"),
    (lambda first, retry: retry["results"][0]["judgement"].pop("cta_assessment"), "missing=\\['cta_assessment'\\]"),
    (lambda first, retry: retry.update(outputs_sha256="0" * 64), "not judged from these inputs"),
    (lambda first, retry: retry.update(cases_sha256="0" * 64), "not judged from these inputs"),
    (lambda first, retry: retry["results"][0].update(input_sha256="0" * 64), "does not belong to these inputs"),
    (lambda first, retry: retry["results"][0].update(body_sha256="0" * 64), "judged on a different body"),
    (lambda first, retry: retry["meta"].update(model="another-model"), "model 'another-model' differs"),
    (lambda first, retry: retry["meta"].update(system_prompt_sha256="0" * 64), "system_prompt_sha256"),
])
def test_merge_rejects_inconsistent_artifacts(frozen: dict[str, Any], outputs: dict[str, Any], break_it: Any, message: str) -> None:
    first, retry = partial_runs(frozen, outputs)
    break_it(first, retry)
    with pytest.raises((lj.InputMismatchError, lj.JudgeResponseError), match=message):
        lj.merge_judgements(frozen, outputs, [("full.json", first), ("retry.json", retry)])


def test_cli_merge_writes_a_new_immutable_file(frozen: dict[str, Any], outputs: dict[str, Any], tmp_path: Path) -> None:
    first, retry = partial_runs(frozen, outputs)
    for name, payload in (("cases.json", frozen), ("outputs.json", outputs), ("full.json", first), ("retry.json", retry)):
        lj.write_new(tmp_path / name, payload)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    args = ["merge", "--cases", str(tmp_path / "cases.json"), "--outputs", str(tmp_path / "outputs.json"),
            "--judgements", str(tmp_path / "full.json"), str(tmp_path / "retry.json"), "--output", str(tmp_path / "merged.json")]

    assert lj.main(args) == 0
    assert lj.main(args) == 2
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name != "merged.json"} == before
    assert [r["status"] for r in json.loads((tmp_path / "merged.json").read_text())["results"]] == ["ok", "ok"]


# --------------------------------------------------------------------------- #
# Reports stay diagnostic
# --------------------------------------------------------------------------- #


def test_reports_have_no_combined_score_or_ranking(frozen: dict[str, Any], outputs: dict[str, Any]) -> None:
    judged = lj.judge_cases(frozen, outputs, lj.FixtureProvider(FIXTURES), log=lambda _: None)
    report = lj.render_report(outputs, judged)
    comparison, _ = lj.render_compare(outputs, outputs, judged, judged)

    for text in (report, comparison):
        assert "not the official challenge score" in text
        for word in ("overall", "combined score", "rank:", "ranking:", "winner", "total score", "/50"):
            assert word not in text.lower().replace("never combined", "").replace("not ranked", "")
    assert report.index("### S03") < report.index("### S10")
    assert "| Specificity | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |" in report
    assert "body unchanged (score differences are judge variance)" in comparison


# --------------------------------------------------------------------------- #
# Production isolation
# --------------------------------------------------------------------------- #


def test_production_code_never_imports_the_evaluator() -> None:
    for path in (ROOT / "app").rglob("*.py"):
        source = path.read_text()
        for forbidden in ("llm_judge", "evaluate_vera", "generativelanguage", "VERA_EVAL", "urllib",
                          "import judge_simulator", "from judge_simulator"):
            assert forbidden not in source, f"{path.relative_to(ROOT)} mentions {forbidden}"
