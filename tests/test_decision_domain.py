import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.engine import (
    CUSTOMER_ACTIONS,
    SEND_AS_BY_SCOPE,
    ActionType,
    CTAType,
    DecisionCandidate,
    DecisionPlan,
    DecisionScope,
    Evidence,
    EvidenceSource,
    SendAs,
    TriggerArchetype,
    is_grounded,
    make_plan_id,
    resolve_field,
)
from app.models import enums as api_enums
from tests.conftest import (
    CANDIDATE_FEATURES,
    candidate_fields,
    load_seed_dataset,
    merchant_payload,
    plan_fields,
    requires_dataset,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def evidence(**overrides: Any) -> Evidence:
    fields: dict[str, Any] = {
        "source": "merchant",
        "field": "performance.delta_7d.calls_pct",
        "value": -0.05,
        "formatted": "calls down 5% vs previous 7 days",
        "importance": 0.7,
    }
    fields.update(overrides)
    return Evidence(**fields)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


def test_trigger_archetype_members() -> None:
    assert {m.name for m in TriggerArchetype} == {
        "SAFETY_COMPLIANCE", "ACTIVE_INTENT", "CUSTOMER_TIMING", "PERFORMANCE",
        "MARKET_OPPORTUNITY", "COMPETITIVE", "OPERATIONS",
    }


def test_action_type_members() -> None:
    assert {m.name for m in ActionType} == {
        "NO_ACTION",
        "SEND_INSIGHT", "SEND_ALERT",
        "DRAFT_CAMPAIGN", "DRAFT_LISTING", "DRAFT_POST", "DRAFT_MESSAGE", "DRAFT_ARTIFACT",
        "SEND_CUSTOMER_REMINDER", "SEND_CUSTOMER_WINBACK", "SEND_CUSTOMER_FOLLOWUP",
        "RECOMMEND_RETENTION", "RECOMMEND_OPERATIONAL_FIX",
        "ASK_MERCHANT",
    }


def test_customer_actions_are_exactly_the_customer_sends() -> None:
    assert CUSTOMER_ACTIONS == {a for a in ActionType if a.name.startswith("SEND_CUSTOMER_")}
    assert all(a.targets_customer for a in CUSTOMER_ACTIONS)
    assert not ActionType.NO_ACTION.targets_customer


def test_decision_scope_members() -> None:
    assert {m.name: m.value for m in DecisionScope} == {"MERCHANT": "merchant", "CUSTOMER": "customer"}


def test_cta_type_members() -> None:
    assert {m.name for m in CTAType} == {"NONE", "YES_NO", "OPEN_ENDED", "CONFIRMATION"}


def test_send_as_is_the_shared_challenge_vocabulary() -> None:
    assert SendAs is api_enums.SendAs
    assert {m.value for m in SendAs} >= {"vera", "merchant_on_behalf"}
    assert SEND_AS_BY_SCOPE == {DecisionScope.MERCHANT: SendAs.VERA, DecisionScope.CUSTOMER: SendAs.MERCHANT_ON_BEHALF}


def test_evidence_source_members() -> None:
    assert {m.value for m in EvidenceSource} == {"category", "merchant", "customer", "trigger", "conversation"}


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #


def test_evidence_valid() -> None:
    ev = evidence()

    assert ev.source is EvidenceSource.MERCHANT
    assert ev.field == "performance.delta_7d.calls_pct"
    assert ev.value == -0.05
    assert ev.importance == 0.7


@pytest.mark.parametrize("importance", [0, 0.0, 1, 1.0])
def test_evidence_importance_bounds_inclusive(importance: float) -> None:
    assert evidence(importance=importance).importance == float(importance)


@pytest.mark.parametrize("importance", [-0.01, -1, 1.01, 2, float("nan"), float("inf"), True, "0.5", None])
def test_evidence_importance_rejected(importance: Any) -> None:
    with pytest.raises(ValidationError):
        evidence(importance=importance)


@pytest.mark.parametrize(
    "override",
    [
        {"source": "llm"},
        {"source": "assumed"},
        {"field": ""},
        {"field": "performance..calls"},
        {"field": "performance calls"},
        {"formatted": ""},
        {"value": object()},
        {"unexpected": "x"},
    ],
)
def test_evidence_invalid_fields_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        evidence(**override)


def test_evidence_preserves_structured_value() -> None:
    value = {"title": "Dental Cleaning @ ₹299", "status": "active", "tags": [1, None, True]}

    assert evidence(field="offers.0", value=value).value == value


def test_evidence_is_frozen() -> None:
    ev = evidence()

    with pytest.raises(ValidationError):
        ev.importance = 0.1


def test_resolve_field_walks_mappings_and_lists() -> None:
    payload = merchant_payload()

    assert resolve_field(payload, "performance.calls") == 18
    assert resolve_field(payload, "offers.0.title") == "Dental Cleaning @ ₹299"
    assert resolve_field(payload, "signals.1") == "ctr_below_peer_median"


@pytest.mark.parametrize("path", ["performance.missing", "offers.5.title", "offers.x", "identity.name.0", "signals.-1"])
def test_resolve_field_missing_path_raises(path: str) -> None:
    with pytest.raises(KeyError):
        resolve_field(merchant_payload(), path)


def test_is_grounded_accepts_real_value() -> None:
    assert is_grounded(evidence(field="performance.calls", value=18, formatted="18 calls in 30 days"), merchant_payload())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("performance.calls", 19),
        ("performance.calls", 18.0),
        ("identity.verified", 1),
        ("performance.bookings", 7),
        ("offers.3.title", "Free Consultation"),
    ],
)
def test_is_grounded_rejects_fabricated_or_mistyped_value(field: str, value: Any) -> None:
    assert not is_grounded(evidence(field=field, value=value), merchant_payload())


@requires_dataset
def test_is_grounded_against_official_seed_merchant() -> None:
    merchants = dict(load_seed_dataset()["merchant"])
    meera = merchants["m_001_drmeera_dentist_delhi"]

    ev = evidence(field="customer_aggregate.high_risk_adult_count", value=124, formatted="124 high-risk adult patients")

    assert is_grounded(ev, meera)


# --------------------------------------------------------------------------- #
# DecisionCandidate
# --------------------------------------------------------------------------- #


def test_candidate_valid() -> None:
    candidate = DecisionCandidate(**candidate_fields(evidence=[evidence()]))

    assert candidate.archetype is TriggerArchetype.MARKET_OPPORTUNITY
    assert candidate.scope is DecisionScope.MERCHANT
    assert candidate.action is ActionType.SEND_INSIGHT
    assert candidate.evidence == (evidence(),)
    assert all(getattr(candidate, f) == 0.5 for f in CANDIDATE_FEATURES)


def test_candidate_optional_fields_default_to_none() -> None:
    candidate = DecisionCandidate(**candidate_fields())

    assert candidate.customer_id is None
    assert candidate.selected_offer_id is None
    assert candidate.expires_at is None
    assert candidate.evidence == ()


def test_candidate_optional_fields_accept_values() -> None:
    expires = datetime(2026, 5, 3, tzinfo=UTC)
    candidate = DecisionCandidate(
        **candidate_fields(customer_id="c_001_priya_for_m001", selected_offer_id="o_meera_001", expires_at=expires)
    )

    assert (candidate.customer_id, candidate.selected_offer_id, candidate.expires_at) == (
        "c_001_priya_for_m001", "o_meera_001", expires,
    )


@pytest.mark.parametrize("feature", CANDIDATE_FEATURES)
@pytest.mark.parametrize("bad", [-0.1, 1.7, float("nan"), float("inf"), True, "0.5", None])
def test_candidate_feature_outside_unit_interval_rejected(feature: str, bad: Any) -> None:
    with pytest.raises(ValidationError) as exc_info:
        DecisionCandidate(**candidate_fields(**{feature: bad}))

    assert exc_info.value.errors()[0]["loc"] == (feature,)


@pytest.mark.parametrize("feature", CANDIDATE_FEATURES)
@pytest.mark.parametrize("edge", [0, 0.0, 1, 1.0])
def test_candidate_feature_bounds_inclusive(feature: str, edge: float) -> None:
    assert getattr(DecisionCandidate(**candidate_fields(**{feature: edge})), feature) == float(edge)


@pytest.mark.parametrize("missing", ["trigger_id", "archetype", "scope", "merchant_id", "objective", "action", "suppression_key", *CANDIDATE_FEATURES])
def test_candidate_required_fields(missing: str) -> None:
    fields = candidate_fields()
    del fields[missing]

    with pytest.raises(ValidationError):
        DecisionCandidate(**fields)


def test_candidate_rejects_naive_expiry() -> None:
    with pytest.raises(ValidationError):
        DecisionCandidate(**candidate_fields(expires_at=datetime(2026, 5, 3)))


def test_candidate_is_frozen_and_forbids_extra_fields() -> None:
    candidate = DecisionCandidate(**candidate_fields())

    with pytest.raises(ValidationError):
        candidate.urgency = 1.0
    with pytest.raises(ValidationError):
        DecisionCandidate(**candidate_fields(score=99.0))


CUSTOMER_SCOPE = {"scope": "customer", "send_as": "merchant_on_behalf"}


def test_customer_scope_requires_customer_id() -> None:
    with pytest.raises(ValidationError, match="requires customer_id"):
        DecisionCandidate(**candidate_fields(**CUSTOMER_SCOPE, action="send_customer_reminder"))


def test_customer_action_requires_customer_scope() -> None:
    with pytest.raises(ValidationError, match="requires scope=customer"):
        DecisionCandidate(**candidate_fields(action="send_customer_reminder", customer_id="c_001_priya_for_m001"))


def test_customer_scope_rejects_merchant_facing_action() -> None:
    with pytest.raises(ValidationError, match="cannot target a customer"):
        DecisionCandidate(**candidate_fields(**CUSTOMER_SCOPE, customer_id="c_001_priya_for_m001", action="draft_post"))


@pytest.mark.parametrize("action", ["send_customer_reminder", "send_customer_winback", "send_customer_followup", "no_action"])
def test_customer_scope_accepts_customer_actions_and_no_action(action: str) -> None:
    cta = "none" if action == "no_action" else "confirmation"
    candidate = DecisionCandidate(
        **candidate_fields(**CUSTOMER_SCOPE, customer_id="c_001_priya_for_m001", action=action, cta_type=cta)
    )

    assert candidate.scope is DecisionScope.CUSTOMER


@pytest.mark.parametrize(
    "override",
    [
        {"send_as": "merchant_on_behalf"},
        {"scope": "customer", "customer_id": "c_1", "action": "send_customer_reminder", "send_as": "vera"},
    ],
)
def test_candidate_send_as_must_match_scope(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="inconsistent with scope"):
        DecisionCandidate(**candidate_fields(**override))


def test_no_action_candidate_requires_no_cta() -> None:
    with pytest.raises(ValidationError, match="requires cta_type=none"):
        DecisionCandidate(**candidate_fields(action="no_action", cta_type="yes_no"))


def test_merchant_scope_may_reference_a_customer() -> None:
    candidate = DecisionCandidate(**candidate_fields(action="ask_merchant", customer_id="c_001_priya_for_m001"))

    assert candidate.customer_id == "c_001_priya_for_m001"


# --------------------------------------------------------------------------- #
# DecisionPlan
# --------------------------------------------------------------------------- #


def test_plan_valid() -> None:
    facts = ["calls fell 5% versus the previous 7 days", "merchant has an active offer"]
    plan = DecisionPlan(
        **plan_fields(
            evidence=[evidence()],
            selected_offer_id="o_meera_001",
            language_style="hi-en mix",
            tone_profile="peer_clinical",
            rationale_facts=facts,
            expires_at=datetime(2026, 5, 3, tzinfo=UTC),
        )
    )

    assert plan.plan_id == make_plan_id(
        merchant_id="m_001_drmeera_dentist_delhi",
        customer_id=None,
        trigger_id="trg_001_research_digest_dentists",
        objective="share_research_digest",
        action=ActionType.SEND_INSIGHT,
        suppression_key="research:dentists:2026-W17",
    )
    assert plan.cta_type is CTAType.OPEN_ENDED
    assert plan.send_as is SendAs.VERA
    assert plan.rationale_facts == tuple(facts)
    assert not plan.is_no_action


@pytest.mark.parametrize("score", [0, 0.0, 50.25, 100, 100.0])
def test_plan_priority_score_bounds_inclusive(score: float) -> None:
    assert DecisionPlan(**plan_fields(priority_score=score)).priority_score == float(score)


@pytest.mark.parametrize("score", [-0.01, 100.01, 150, float("nan"), float("inf"), True, "50"])
def test_plan_priority_score_rejected(score: Any) -> None:
    with pytest.raises(ValidationError):
        DecisionPlan(**plan_fields(priority_score=score))


@pytest.mark.parametrize("confidence", [0, 0.0, 0.5, 1, 1.0])
def test_plan_confidence_bounds_inclusive(confidence: float) -> None:
    assert DecisionPlan(**plan_fields(confidence=confidence)).confidence == float(confidence)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan"), True])
def test_plan_confidence_rejected(confidence: Any) -> None:
    with pytest.raises(ValidationError):
        DecisionPlan(**plan_fields(confidence=confidence))


def test_no_action_plan_is_valid() -> None:
    plan = DecisionPlan(
        **plan_fields(
            action="no_action",
            cta_type="none",
            priority_score=0.0,
            confidence=0.9,
            rationale_facts=["Saturday IPL matches shift covers to home viewing"],
        )
    )

    assert plan.is_no_action
    assert plan.action is ActionType.NO_ACTION


def test_no_action_plan_cannot_carry_a_cta() -> None:
    with pytest.raises(ValidationError, match="requires cta_type=none"):
        DecisionPlan(**plan_fields(action="no_action", cta_type="yes_no"))


def test_customer_plan_with_optional_fields() -> None:
    plan = DecisionPlan(
        **plan_fields(
            scope="customer",
            customer_id="c_001_priya_for_m001",
            action="send_customer_reminder",
            send_as="merchant_on_behalf",
            cta_type="confirmation",
            selected_offer_id="o_meera_001",
            suppression_key="recall:c_001_priya_for_m001:6mo",
        )
    )

    assert plan.customer_id == "c_001_priya_for_m001"
    assert plan.selected_offer_id == "o_meera_001"


def test_plan_optional_fields_default_to_none_or_empty() -> None:
    plan = DecisionPlan(**plan_fields())

    assert (plan.customer_id, plan.selected_offer_id, plan.language_style, plan.tone_profile, plan.expires_at) == (None,) * 5
    assert plan.evidence == ()
    assert plan.rationale_facts == ()


@pytest.mark.parametrize(
    "override",
    [
        {"send_as": "merchant_on_behalf"},
        {"scope": "customer", "customer_id": "c_1", "action": "send_customer_reminder", "send_as": "vera"},
    ],
)
def test_plan_send_as_must_match_scope(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="inconsistent with scope"):
        DecisionPlan(**plan_fields(**override))


@pytest.mark.parametrize("fact", ["", None])
def test_plan_rejects_empty_rationale_fact(fact: Any) -> None:
    with pytest.raises(ValidationError):
        DecisionPlan(**plan_fields(rationale_facts=["valid fact", fact]))


def test_plan_is_frozen() -> None:
    plan = DecisionPlan(**plan_fields())

    with pytest.raises(ValidationError):
        plan.priority_score = 99.0


def test_plan_round_trips_through_json() -> None:
    plan = DecisionPlan(**plan_fields(evidence=[evidence()], rationale_facts=["fact"], expires_at="2026-05-03T00:00:00Z"))

    assert DecisionPlan.model_validate_json(plan.model_dump_json()) == plan
    assert DecisionPlan.model_validate(json.loads(plan.model_dump_json())) == plan


# --------------------------------------------------------------------------- #
# Plan IDs
# --------------------------------------------------------------------------- #


def test_identical_decisions_share_plan_id() -> None:
    assert DecisionPlan(**plan_fields()).plan_id == DecisionPlan(**plan_fields()).plan_id


def test_plan_id_format() -> None:
    assert re.fullmatch(r"plan_[0-9a-f]{20}", DecisionPlan(**plan_fields()).plan_id)


def test_plan_id_is_stable_across_processes() -> None:
    code = (
        "from app.engine import make_plan_id; "
        "print(make_plan_id(merchant_id='m', customer_id=None, trigger_id='t', objective='o', "
        "action='send_insight', suppression_key='k'))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)

    assert out.stdout.strip() == make_plan_id(
        merchant_id="m", customer_id=None, trigger_id="t", objective="o", action="send_insight", suppression_key="k"
    )


@pytest.mark.parametrize(
    "override",
    [
        {"merchant_id": "m_002_bharat_dentist_mumbai"},
        {"trigger_id": "trg_002"},
        {"objective": "recover_call_volume"},
        {"action": "draft_post"},
        {"suppression_key": "research:dentists:2026-W18"},
        {"customer_id": "c_001_priya_for_m001"},
    ],
)
def test_plan_id_changes_with_identity_fields(override: dict[str, Any]) -> None:
    assert DecisionPlan(**plan_fields(**override)).plan_id != DecisionPlan(**plan_fields()).plan_id


@pytest.mark.parametrize(
    "override",
    [
        {"priority_score": 10.0},
        {"confidence": 0.1},
        {"cta_type": "yes_no"},
        {"tone_profile": "peer_clinical"},
        {"rationale_facts": ["different fact"]},
        {"selected_offer_id": "o_meera_001"},
        {"archetype": "performance"},
    ],
)
def test_plan_id_ignores_non_identity_fields(override: dict[str, Any]) -> None:
    assert DecisionPlan(**plan_fields(**override)).plan_id == DecisionPlan(**plan_fields()).plan_id


def test_plan_id_accepts_enum_or_string_action() -> None:
    common = {"merchant_id": "m", "customer_id": None, "trigger_id": "t", "objective": "o", "suppression_key": "k"}

    assert make_plan_id(action=ActionType.DRAFT_POST, **common) == make_plan_id(action="draft_post", **common)


def test_plan_id_has_no_separator_collisions() -> None:
    common = {"customer_id": None, "trigger_id": "t", "action": "send_insight", "suppression_key": "k"}

    assert make_plan_id(merchant_id="a|b", objective="c", **common) != make_plan_id(merchant_id="a", objective="b|c", **common)


def test_supplied_matching_plan_id_is_accepted() -> None:
    derived = DecisionPlan(**plan_fields()).plan_id

    assert DecisionPlan(**plan_fields(plan_id=derived)).plan_id == derived


def test_forged_plan_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="plan_id does not match"):
        DecisionPlan(**plan_fields(plan_id="plan_00000000000000000000"))


# --------------------------------------------------------------------------- #
# Independence from the web layer
# --------------------------------------------------------------------------- #


def test_engine_imports_without_fastapi() -> None:
    code = (
        "import sys, app.engine; "
        "leaked = sorted(m for m in ('fastapi', 'starlette', 'uvicorn', 'httpx') if m in sys.modules); "
        "print(','.join(leaked))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)

    assert out.stdout.strip() == ""
