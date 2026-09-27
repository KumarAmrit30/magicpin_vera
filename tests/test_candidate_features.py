"""Feature extraction for decision candidates (Phase 2B)."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.engine.actions import ActionType, DecisionScope
from app.engine.evidence import Evidence, EvidenceSource
from app.engine.features import (
    actionability,
    addresses_trigger_subject,
    compute_features,
    conversation_relevance,
    engagement_potential,
    evidence_strength,
    merchant_engaged,
    merchant_relevance,
    merchant_unresponsive,
    open_merchant_request,
    time_pressure,
    tokens,
    urgency,
)
from tests.conftest import SEED_NOW, requires_dataset, seed_context, seed_context_parts
from app.engine.candidates import CandidateGenerationContext

pytestmark = requires_dataset


def ev(source: EvidenceSource = EvidenceSource.TRIGGER, importance: float = 0.5, field: str = "kind") -> Evidence:
    return Evidence(source=source, field=field, value="x", formatted="x", importance=importance)


def ctx_with(trigger_id: str, **trigger_overrides) -> CandidateGenerationContext:
    parts = seed_context_parts(trigger_id)
    parts["trigger"].update(trigger_overrides)
    return CandidateGenerationContext(**parts, now=SEED_NOW)


# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #


def test_tokens_normalise_words() -> None:
    assert tokens("corporate_bulk_thali_package") == {"corporate", "bulk", "thali", "package"}
    assert tokens("Dental Cleaning @ ₹299", None, 5) == {"dental", "cleaning"}
    assert tokens("high_risk_adults") == {"high", "risk", "adult"}
    assert tokens("the and for 2026") == frozenset()


# --------------------------------------------------------------------------- #
# urgency / time pressure
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("level", "expected"), [(1, 0.2), (3, 0.6), (5, 1.0)])
def test_urgency_scales_trigger_urgency(level: int, expected: float) -> None:
    assert urgency(ctx_with("trg_004_perf_dip_bharat", urgency=level), ActionType.SEND_ALERT) == expected


def test_urgency_is_zero_when_absent_or_for_no_action() -> None:
    ctx = ctx_with("trg_004_perf_dip_bharat", urgency=None)

    assert urgency(ctx, ActionType.SEND_ALERT) == 0.0
    assert urgency(seed_context("trg_018_supply_atorvastatin_recall"), ActionType.NO_ACTION) == 0.0


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (timedelta(hours=2), 1.0),
        (timedelta(hours=48), 0.8),
        (timedelta(days=5), 0.6),
        (timedelta(days=10), 0.4),
        (timedelta(days=20), 0.2),
        (timedelta(days=90), 0.0),
        (timedelta(hours=-1), 0.0),
    ],
)
def test_time_pressure_steps(remaining: timedelta, expected: float) -> None:
    ctx = ctx_with("trg_004_perf_dip_bharat", expires_at=(SEED_NOW + remaining).isoformat())

    assert time_pressure(ctx, ActionType.SEND_ALERT) == expected


def test_time_pressure_uses_nearest_deadline() -> None:
    ctx = ctx_with("trg_004_perf_dip_bharat", expires_at=(SEED_NOW + timedelta(days=90)).isoformat())

    assert time_pressure(ctx, ActionType.SEND_ALERT, SEED_NOW + timedelta(hours=3)) == 1.0


def test_time_pressure_zero_without_deadline_or_for_no_action() -> None:
    ctx = ctx_with("trg_004_perf_dip_bharat", expires_at=None)

    assert time_pressure(ctx, ActionType.SEND_ALERT) == 0.0
    assert time_pressure(seed_context("trg_010_ipl_match_delhi"), ActionType.NO_ACTION, SEED_NOW) == 0.0


# --------------------------------------------------------------------------- #
# relevance / actionability / evidence
# --------------------------------------------------------------------------- #


def test_merchant_relevance_counts_merchant_and_customer_facts() -> None:
    assert merchant_relevance([ev()]) == 0.3
    assert merchant_relevance([ev(EvidenceSource.MERCHANT), ev(EvidenceSource.CUSTOMER), ev(EvidenceSource.CATEGORY)]) == 0.5
    assert merchant_relevance([ev(EvidenceSource.MERCHANT)] * 20) == 1.0
    assert merchant_relevance([ev()], adjustment=-1.0) == 0.0


def test_addressing_another_party_than_the_trigger_forgoes_one_fact_step() -> None:
    facts = [ev(EvidenceSource.MERCHANT), ev(EvidenceSource.CUSTOMER)]

    assert merchant_relevance(facts) == merchant_relevance(facts, addresses_trigger_subject=True) == 0.5
    assert merchant_relevance(facts, addresses_trigger_subject=False) == 0.4
    assert merchant_relevance([ev()], adjustment=-1.0, addresses_trigger_subject=False) == 0.0
    assert merchant_relevance([ev(EvidenceSource.MERCHANT)] * 20, addresses_trigger_subject=False) == 1.0


@pytest.mark.parametrize(
    ("trigger_id", "trigger_scope"),
    [("trg_003_recall_due_priya", "customer"), ("trg_004_perf_dip_bharat", "merchant")],
)
def test_trigger_subject_is_the_declared_trigger_scope_in_both_directions(trigger_id: str, trigger_scope: str) -> None:
    ctx = seed_context(trigger_id)
    other = DecisionScope.MERCHANT if trigger_scope == "customer" else DecisionScope.CUSTOMER

    assert ctx.trigger["scope"] == trigger_scope
    assert addresses_trigger_subject(ctx, DecisionScope(trigger_scope))
    assert not addresses_trigger_subject(ctx, other)


@pytest.mark.parametrize("declared", [None, "", "everyone"])
def test_every_context_declares_a_trigger_scope(declared: str | None) -> None:
    with pytest.raises(ValidationError, match="scope"):
        ctx_with("trg_003_recall_due_priya", scope=declared)


def test_compute_features_discounts_only_merchant_relevance_for_the_other_party() -> None:
    ctx = seed_context("trg_003_recall_due_priya")
    kwargs = dict(action=ActionType.DRAFT_MESSAGE, evidence=[ev(EvidenceSource.CUSTOMER, 0.9)], topic=tokens("recall"))

    direct = compute_features(ctx, **kwargs, scope=DecisionScope.CUSTOMER)
    indirect = compute_features(ctx, **kwargs, scope=DecisionScope.MERCHANT)

    assert compute_features(ctx, **kwargs) == direct
    assert (direct.merchant_relevance, indirect.merchant_relevance) == (0.4, 0.3)
    assert {k: v for k, v in indirect.as_fields().items() if k != "merchant_relevance"} == {
        k: v for k, v in direct.as_fields().items() if k != "merchant_relevance"
    }


def test_actionability_rises_with_assets() -> None:
    assert actionability(ActionType.DRAFT_CAMPAIGN) == 0.4
    assert actionability(ActionType.DRAFT_CAMPAIGN, assets=2) == 0.7
    assert actionability(ActionType.ASK_MERCHANT, assets=5) == 1.0
    assert actionability(ActionType.NO_ACTION, assets=3) == 1.0


def test_evidence_strength_rewards_peak_and_breadth() -> None:
    kind_only = evidence_strength([ev(importance=0.2)])
    core_only = evidence_strength([ev(importance=0.2), ev(importance=0.9)])
    corroborated = evidence_strength([ev(importance=0.9)] + [ev(importance=0.4)] * 4)

    assert evidence_strength([]) == 0.0
    assert kind_only == 0.14
    assert kind_only < core_only < corroborated
    assert corroborated == 0.93


# --------------------------------------------------------------------------- #
# conversation state
# --------------------------------------------------------------------------- #


def test_engagement_states_from_history() -> None:
    engaged = seed_context("trg_018_supply_atorvastatin_recall")  # merchant said "Yes send me the list please"
    unanswered = seed_context("trg_025_dormancy_glamour")  # Vera's last message went unanswered

    assert merchant_engaged(engaged) and not merchant_unresponsive(engaged)
    assert merchant_unresponsive(unanswered) and not merchant_engaged(unanswered)


def test_open_merchant_request_only_when_merchant_spoke_last_with_intent() -> None:
    assert open_merchant_request(seed_context("trg_012_milestone_mylari")).body == "Yes good idea, what would it look like"
    assert open_merchant_request(seed_context("trg_016_kids_yoga_program_drafting")) is None  # Vera replied last
    assert open_merchant_request(seed_context("trg_008_curious_ask_studio11")) is None


def test_conversation_relevance_levels() -> None:
    engaged = seed_context("trg_018_supply_atorvastatin_recall")
    cold = seed_context("trg_004_perf_dip_bharat")
    empty = seed_context("trg_021_unverified_gbp_sunrise")
    topic = tokens("atorvastatin")

    assert conversation_relevance(engaged, ActionType.DRAFT_MESSAGE, topic) == 0.9
    assert conversation_relevance(engaged, ActionType.SEND_ALERT, topic) == 0.3
    assert conversation_relevance(engaged, ActionType.DRAFT_MESSAGE, tokens("unrelated")) == 0.3
    assert conversation_relevance(cold, ActionType.SEND_ALERT, tokens("subscription")) == 0.6
    assert conversation_relevance(cold, ActionType.SEND_ALERT, tokens("unrelated")) == 0.1
    assert conversation_relevance(empty, ActionType.SEND_ALERT, topic) == 0.0
    assert conversation_relevance(empty, ActionType.DRAFT_ARTIFACT, topic, continues_request=True) == 1.0
    assert conversation_relevance(engaged, ActionType.NO_ACTION, topic, continues_request=True) == 0.0


def test_engagement_potential_follows_recipient() -> None:
    engaged = seed_context("trg_018_supply_atorvastatin_recall")
    dormant = seed_context("trg_025_dormancy_glamour")
    lapsed = seed_context("trg_015_winback_rashmi")

    assert engagement_potential(engaged, ActionType.ASK_MERCHANT) == 0.8
    assert engagement_potential(dormant, ActionType.ASK_MERCHANT) == 0.4
    assert engagement_potential(lapsed, ActionType.SEND_CUSTOMER_WINBACK) == 0.3
    assert engagement_potential(engaged, ActionType.NO_ACTION) == 0.0


def test_compute_features_is_in_unit_interval_and_deterministic() -> None:
    ctx = seed_context("trg_010_ipl_match_delhi")
    kwargs = dict(action=ActionType.DRAFT_CAMPAIGN, evidence=[ev(EvidenceSource.MERCHANT, 0.9)], topic=tokens("ipl"), assets=9)

    features = compute_features(ctx, **kwargs)

    assert all(0.0 <= v <= 1.0 for v in features.as_fields().values())
    assert compute_features(ctx, **kwargs) == features


def test_time_pressure_override_does_not_apply_to_no_action() -> None:
    ctx = seed_context("trg_004_perf_dip_bharat")

    assert compute_features(ctx, action=ActionType.SEND_ALERT, evidence=[ev()], time_pressure_override=1.0).time_pressure == 1.0
    assert compute_features(ctx, action=ActionType.NO_ACTION, evidence=[ev()], time_pressure_override=1.0).time_pressure == 0.0
