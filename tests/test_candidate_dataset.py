"""Candidate generation against the canonical case studies and the full expanded dataset (Phase 2B).

The expanded dataset is produced by the official ``generate_dataset.py`` into a
pytest temporary directory, so nothing is written into the repository.
"""

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from app.engine import ActionType, CTAType, DecisionScope, SendAs, TRIGGER_KIND_ARCHETYPES, classify_trigger
from app.engine.candidates import CandidateGenerationContext, generate_candidates
from tests.conftest import DATASET_DIR, SEED_NOW, requires_dataset, seed_context

pytestmark = requires_dataset

A = ActionType


# --------------------------------------------------------------------------- #
# Canonical case studies (examples/case-studies.md)
# --------------------------------------------------------------------------- #


def _actions(trigger_id: str) -> dict[ActionType, list]:
    grouped: dict[ActionType, list] = {}
    for candidate in generate_candidates(seed_context(trigger_id)):
        grouped.setdefault(candidate.action, []).append(candidate)
    return grouped


def test_case_1_research_digest() -> None:
    found = _actions("trg_001_research_digest_dentists")

    assert A.SEND_INSIGHT in found and A.DRAFT_ARTIFACT in found
    insight = found[A.SEND_INSIGHT][0]
    assert insight.send_as is SendAs.VERA
    assert any(e.value == 124 for e in insight.evidence)  # high-risk adult cohort from merchant data


def test_case_2_recall_reminder() -> None:
    reminder = _actions("trg_003_recall_due_priya")[A.SEND_CUSTOMER_REMINDER][0]

    assert reminder.send_as is SendAs.MERCHANT_ON_BEHALF
    assert reminder.selected_offer_id == "o_meera_001"
    assert reminder.cta_type is CTAType.CONFIRMATION


def test_case_3_bridal_followup() -> None:
    followup = _actions("trg_007_bridal_followup_kavya")[A.SEND_CUSTOMER_FOLLOWUP][0]

    assert followup.scope is DecisionScope.CUSTOMER
    assert {e.value for e in followup.evidence} >= {196, "skin_prep_program_30day"}


def test_case_4_curious_ask() -> None:
    found = _actions("trg_008_curious_ask_studio11")

    assert list(found) == [A.ASK_MERCHANT]
    assert found[A.ASK_MERCHANT][0].cta_type is CTAType.OPEN_ENDED


def test_case_5_ipl_contrarian() -> None:
    found = _actions("trg_010_ipl_match_delhi")

    assert set(found) == {A.SEND_INSIGHT, A.DRAFT_CAMPAIGN}
    assert found[A.DRAFT_CAMPAIGN][0].selected_offer_id == "o_skpz_001"  # the BOGO already running
    assert any(e.value is False and e.field == "payload.is_weeknight" for e in found[A.SEND_INSIGHT][0].evidence)


def test_case_6_corporate_thali_draft() -> None:
    found = _actions("trg_013_corporate_thali_planning")

    assert A.ASK_MERCHANT not in found
    assert found[A.DRAFT_ARTIFACT][0].conversation_relevance == 1.0


def test_case_7_seasonal_reframe() -> None:
    found = _actions("trg_014_seasonal_acquisition_dip_powerhouse")

    assert A.SEND_ALERT not in found
    assert set(found) == {A.SEND_INSIGHT, A.RECOMMEND_RETENTION, A.NO_ACTION}
    assert any(e.value == 245 for e in found[A.RECOMMEND_RETENTION][0].evidence)


def test_case_8_customer_winback() -> None:
    winback = _actions("trg_015_winback_rashmi")[A.SEND_CUSTOMER_WINBACK][0]

    assert winback.send_as is SendAs.MERCHANT_ON_BEHALF
    assert {e.value for e in winback.evidence} >= {57, "weight_loss", 5}


def test_case_9_supply_recall() -> None:
    found = _actions("trg_018_supply_atorvastatin_recall")
    alert = found[A.SEND_ALERT][0]

    assert alert.urgency == 1.0
    assert any(e.value == ["AT2024-1102", "AT2024-1108"] for e in alert.evidence)
    assert any(e.value == 240 for e in found[A.DRAFT_MESSAGE][0].evidence)


def test_case_10_chronic_refill() -> None:
    reminder = _actions("trg_019_chronic_refill_grandfather")[A.SEND_CUSTOMER_REMINDER][0]

    assert reminder.cta_type is CTAType.CONFIRMATION
    assert any(e.field == "payload.molecule_list" for e in reminder.evidence)


# --------------------------------------------------------------------------- #
# Full expanded dataset
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def expanded(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("vera_expanded") / "expanded"
    subprocess.run(
        [sys.executable, str(DATASET_DIR / "generate_dataset.py"), "--seed-dir", str(DATASET_DIR), "--out", str(out)],
        cwd=out.parent,
        check=True,
        capture_output=True,
    )

    def load(sub: str, key: str) -> dict[str, dict]:
        return {data[key]: data for data in (json.loads(p.read_text()) for p in sorted((out / sub).glob("*.json")))}

    return {
        "categories": load("categories", "slug"),
        "merchants": load("merchants", "merchant_id"),
        "customers": load("customers", "customer_id"),
        "triggers": load("triggers", "id"),
        "pairs": json.loads((out / "test_pairs.json").read_text())["pairs"],
    }


def _context(data: dict[str, Any], trigger_id: str) -> CandidateGenerationContext:
    trigger = data["triggers"][trigger_id]
    merchant = data["merchants"][trigger["merchant_id"]]
    customer_id = trigger.get("customer_id")
    return CandidateGenerationContext(
        category=data["categories"][merchant["category_slug"]],
        merchant=merchant,
        trigger=trigger,
        customer=data["customers"][customer_id] if customer_id else None,
        now=SEED_NOW,
    )


def test_expanded_dataset_is_generated_outside_the_repo(expanded: dict[str, Any]) -> None:
    repo = Path(__file__).resolve().parent.parent

    assert not (repo / "expanded").exists()
    assert not (DATASET_DIR / "expanded").exists()
    assert len(expanded["triggers"]) == 100


def test_every_expanded_kind_is_mapped(expanded: dict[str, Any]) -> None:
    kinds = {t["kind"] for t in expanded["triggers"].values()}

    assert kinds == set(TRIGGER_KIND_ARCHETYPES)
    assert all(classify_trigger(t) is not None for t in expanded["triggers"].values())


def test_every_expanded_trigger_yields_valid_grounded_candidates(expanded: dict[str, Any]) -> None:
    counts: Counter[ActionType] = Counter()
    for trigger_id in sorted(expanded["triggers"]):
        ctx = _context(expanded, trigger_id)
        candidates = generate_candidates(ctx)

        assert candidates, trigger_id
        for c in candidates:
            counts[c.action] += 1
            assert c.evidence and all(ctx.is_grounded(e) for e in c.evidence), trigger_id
            assert c.send_as is (SendAs.MERCHANT_ON_BEHALF if c.scope is DecisionScope.CUSTOMER else SendAs.VERA)
            assert c.action is not A.NO_ACTION or c.cta_type is CTAType.NONE
            assert not c.action.targets_customer or c.customer_id == ctx.customer_id
            assert c.selected_offer_id is None or c.selected_offer_id in {o["id"] for _, o in ctx.active_offers}

    assert counts[A.NO_ACTION] < sum(counts.values()) / 2


def test_placeholder_triggers_never_cite_the_placeholder(expanded: dict[str, Any]) -> None:
    for trigger_id, trigger in sorted(expanded["triggers"].items()):
        if not trigger["payload"].get("placeholder"):
            continue
        for candidate in generate_candidates(_context(expanded, trigger_id)):
            assert not any(e.field.startswith("payload.") for e in candidate.evidence), trigger_id


def test_expanded_generation_is_deterministic(expanded: dict[str, Any]) -> None:
    ids = sorted(expanded["triggers"])

    first = [generate_candidates(_context(expanded, tid)) for tid in ids]
    second = [generate_candidates(_context(expanded, tid)) for tid in ids]

    assert first == second


def test_all_canonical_test_pairs_have_candidates(expanded: dict[str, Any]) -> None:
    pairs = expanded["pairs"]

    assert len(pairs) == 30
    for pair in pairs:
        ctx = _context(expanded, pair["trigger_id"])
        assert (ctx.merchant_id, ctx.customer_id) == (pair["merchant_id"], pair["customer_id"])
        assert generate_candidates(ctx), pair["test_id"]
