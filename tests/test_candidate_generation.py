"""Candidate generation: dispatch, invariants, and per-archetype rules (Phase 2B)."""

import copy
from typing import Any

import pytest

from app.engine import (
    ActionType,
    CTAType,
    DecisionCandidate,
    DecisionScope,
    Evidence,
    EvidenceSource,
    SendAs,
    TriggerArchetype,
    classify_trigger,
)
from app.engine.candidates import GENERATORS, ArchetypeGenerator, CandidateGenerationContext, CandidateGenerator, generate_candidates
from app.engine.candidates.base import IMPORTANCE_KIND
from tests.conftest import (
    DATASET_DIR,
    SEED_NOW,
    candidate_fields,
    load_seed_dataset,
    requires_dataset,
    seed_context,
    seed_context_parts,
)

pytestmark = requires_dataset

A = ActionType
SEED_TRIGGER_IDS = [tid for tid, _ in load_seed_dataset()["trigger"]] if DATASET_DIR.is_dir() else []


def ctx_for(trigger_id: str, *, payload: dict[str, Any] | None = None, merchant: dict | None = None, **trigger: Any) -> CandidateGenerationContext:
    """Seed context with trigger fields / payload keys / merchant fields overridden."""
    parts = seed_context_parts(trigger_id)
    parts["trigger"].update(trigger)
    if payload is not None:
        parts["trigger"]["payload"] = payload
    if merchant:
        parts["merchant"].update(merchant)
    return CandidateGenerationContext(**parts, now=SEED_NOW)


def patch_payload(trigger_id: str, **changes: Any) -> CandidateGenerationContext:
    payload = seed_context_parts(trigger_id)["trigger"]["payload"]
    payload.update(changes)
    return ctx_for(trigger_id, payload=payload)


def actions(candidates: list[DecisionCandidate]) -> list[ActionType]:
    return [c.action for c in candidates]


def only(candidates: list[DecisionCandidate], action: ActionType) -> DecisionCandidate:
    matches = [c for c in candidates if c.action is action]
    assert len(matches) == 1, actions(candidates)
    return matches[0]


def fields(candidate: DecisionCandidate) -> set[str]:
    return {e.field for e in candidate.evidence}


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def test_generators_satisfy_protocol() -> None:
    assert all(isinstance(g, CandidateGenerator) for g in GENERATORS.values())


def test_registry_is_read_only() -> None:
    with pytest.raises(TypeError):
        GENERATORS[TriggerArchetype.COMPETITIVE] = GENERATORS[TriggerArchetype.OPERATIONS]  # type: ignore[index]


@pytest.mark.parametrize("kind", ["weather_heatwave", "made_up_kind", "PERF_DIP"])
def test_unknown_kind_yields_no_candidates(kind: str) -> None:
    ctx = ctx_for("trg_004_perf_dip_bharat", kind=kind)

    assert generate_candidates(ctx) == []


def test_unknown_kind_does_not_reach_any_generator() -> None:
    class Exploding:
        archetype = TriggerArchetype.PERFORMANCE

        def generate(self, context):
            raise AssertionError("must not be called")

    ctx = ctx_for("trg_004_perf_dip_bharat", kind="unknown")

    assert generate_candidates(ctx, {a: Exploding() for a in TriggerArchetype}) == []


def test_dispatch_uses_the_classified_archetype() -> None:
    seen: list[TriggerArchetype] = []

    class Recording:
        def __init__(self, archetype: TriggerArchetype) -> None:
            self.archetype = archetype

        def generate(self, context):
            seen.append(self.archetype)
            return []

    generate_candidates(seed_context("trg_023_competitor_opened_dentist"), {a: Recording(a) for a in TriggerArchetype})

    assert seen == [TriggerArchetype.COMPETITIVE]


def _rogue(candidate: DecisionCandidate):
    class Rogue:
        archetype = TriggerArchetype.PERFORMANCE

        def generate(self, context):
            return [candidate]

    return {a: Rogue() for a in TriggerArchetype}


def _candidate_for(ctx: CandidateGenerationContext, **overrides: Any) -> DecisionCandidate:
    base = candidate_fields(
        trigger_id=ctx.trigger_id,
        merchant_id=ctx.merchant_id,
        archetype="performance",
        suppression_key=ctx.suppression_key,
        evidence=[Evidence(source=EvidenceSource.TRIGGER, field="payload.delta_pct", value=-0.5, formatted="calls -50%", importance=0.9)],
    )
    return DecisionCandidate(**{**base, **overrides})


def test_grounded_foreign_candidate_passes_the_gate() -> None:
    ctx = seed_context("trg_004_perf_dip_bharat")

    assert len(generate_candidates(ctx, _rogue(_candidate_for(ctx)))) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence": [Evidence(source=EvidenceSource.TRIGGER, field="payload.delta_pct", value=-0.9, formatted="calls -90%", importance=0.9)]},
        {"evidence": [Evidence(source=EvidenceSource.TRIGGER, field="payload.delta_pct", value="-0.5", formatted="x", importance=0.9)]},
        {"evidence": [Evidence(source=EvidenceSource.MERCHANT, field="identity.competitor", value="X", formatted="X", importance=0.9)]},
        {"evidence": []},
        {"archetype": "competitive"},
        {"trigger_id": "trg_other"},
    ],
)
def test_ungrounded_or_foreign_candidates_are_dropped(overrides: dict[str, Any]) -> None:
    ctx = seed_context("trg_004_perf_dip_bharat")

    assert generate_candidates(ctx, _rogue(_candidate_for(ctx, **overrides))) == []


# --------------------------------------------------------------------------- #
# Invariants over every seed trigger
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("trigger_id", SEED_TRIGGER_IDS)
def test_seed_trigger_candidates_are_valid_and_grounded(trigger_id: str) -> None:
    ctx = seed_context(trigger_id)
    candidates = generate_candidates(ctx)
    archetype = classify_trigger(ctx.trigger)
    active_offer_ids = {offer["id"] for _, offer in ctx.active_offers}

    assert candidates
    for c in candidates:
        assert c.archetype is archetype
        assert (c.trigger_id, c.merchant_id, c.suppression_key, c.expires_at) == (
            ctx.trigger_id,
            ctx.merchant_id,
            ctx.suppression_key,
            ctx.expires_at,
        )
        assert c.send_as is (SendAs.MERCHANT_ON_BEHALF if c.scope is DecisionScope.CUSTOMER else SendAs.VERA)
        assert c.action is not A.NO_ACTION or c.cta_type is CTAType.NONE
        assert not c.action.targets_customer or (c.scope is DecisionScope.CUSTOMER and c.customer_id == ctx.customer_id)
        assert c.selected_offer_id is None or c.selected_offer_id in active_offer_ids
        assert c.evidence and all(ctx.is_grounded(e) for e in c.evidence)
        assert any(e.field == "kind" and e.importance == IMPORTANCE_KIND for e in c.evidence)
        assert list(c.evidence) == sorted(c.evidence, key=lambda e: (-e.importance, e.source.value, e.field))
        assert len({(e.source, e.field) for e in c.evidence}) == len(c.evidence)


@pytest.mark.parametrize("trigger_id", SEED_TRIGGER_IDS)
def test_seed_trigger_candidates_are_distinct(trigger_id: str) -> None:
    candidates = generate_candidates(seed_context(trigger_id))
    identities = [(c.action, c.objective, c.selected_offer_id) for c in candidates]

    assert len(identities) == len(set(identities))


@pytest.mark.parametrize("trigger_id", SEED_TRIGGER_IDS)
def test_generation_is_deterministic(trigger_id: str) -> None:
    first = generate_candidates(seed_context(trigger_id))

    for _ in range(3):
        assert generate_candidates(seed_context(trigger_id)) == first


@pytest.mark.parametrize("trigger_id", SEED_TRIGGER_IDS)
def test_output_order_does_not_depend_on_payload_key_order(trigger_id: str) -> None:
    parts = seed_context_parts(trigger_id)
    reversed_parts = {
        key: (dict(reversed(list(value.items()))) if isinstance(value, dict) else value) for key, value in parts.items()
    }
    reversed_parts["trigger"]["payload"] = dict(reversed(list(parts["trigger"]["payload"].items())))

    left = generate_candidates(CandidateGenerationContext(**parts, now=SEED_NOW))
    right = generate_candidates(CandidateGenerationContext(**reversed_parts, now=SEED_NOW))

    assert left == right


@pytest.mark.parametrize("trigger_id", SEED_TRIGGER_IDS)
def test_generation_does_not_mutate_inputs(trigger_id: str) -> None:
    parts = seed_context_parts(trigger_id)
    snapshot = copy.deepcopy(parts)
    ctx = CandidateGenerationContext(**parts, now=SEED_NOW)
    before = ctx.model_dump()

    generate_candidates(ctx)

    assert parts == snapshot
    assert ctx.model_dump() == before


def test_no_action_is_not_a_fallback_for_every_trigger() -> None:
    with_restraint = {tid for tid in SEED_TRIGGER_IDS if A.NO_ACTION in actions(generate_candidates(seed_context(tid)))}

    assert with_restraint == {"trg_006_festival_diwali", "trg_014_seasonal_acquisition_dip_powerhouse"}


# --------------------------------------------------------------------------- #
# SAFETY_COMPLIANCE
# --------------------------------------------------------------------------- #


def test_regulation_change_alerts_with_cited_regulation_and_deadline() -> None:
    candidates = generate_candidates(seed_context("trg_002_compliance_dci_radiograph"))
    alert = only(candidates, A.SEND_ALERT)

    assert set(actions(candidates)) == {A.SEND_ALERT, A.RECOMMEND_OPERATIONAL_FIX}
    assert {"digest.1.title", "payload.deadline_iso"} <= fields(alert)
    assert "digest.1.actionable" in fields(only(candidates, A.RECOMMEND_OPERATIONAL_FIX))


def test_regulation_without_citable_item_is_restraint() -> None:
    candidates = generate_candidates(patch_payload("trg_002_compliance_dci_radiograph", top_item_id="d_missing"))

    assert actions(candidates) == [A.NO_ACTION]


def test_supply_alert_advances_the_thread_the_merchant_accepted() -> None:
    candidates = generate_candidates(seed_context("trg_018_supply_atorvastatin_recall"))
    alert, draft = only(candidates, A.SEND_ALERT), only(candidates, A.DRAFT_MESSAGE)

    assert set(actions(candidates)) == {A.SEND_ALERT, A.DRAFT_MESSAGE, A.ASK_MERCHANT}
    assert {"payload.molecule", "payload.affected_batches"} <= fields(alert)
    assert "customer_aggregate.chronic_rx_count" in fields(draft)
    assert draft.conversation_relevance > alert.conversation_relevance
    assert alert.urgency == 1.0


def test_supply_alert_without_batches_is_restraint() -> None:
    payload = seed_context_parts("trg_018_supply_atorvastatin_recall")["trigger"]["payload"]
    del payload["affected_batches"]

    assert actions(generate_candidates(ctx_for("trg_018_supply_atorvastatin_recall", payload=payload))) == [A.NO_ACTION]


# --------------------------------------------------------------------------- #
# ACTIVE_INTENT
# --------------------------------------------------------------------------- #


def test_active_planning_drafts_instead_of_asking() -> None:
    candidates = generate_candidates(seed_context("trg_013_corporate_thali_planning"))
    draft = only(candidates, A.DRAFT_ARTIFACT)

    assert A.ASK_MERCHANT not in actions(candidates)
    assert draft.cta_type is CTAType.CONFIRMATION
    assert draft.conversation_relevance == 1.0
    assert {"payload.intent_topic", "payload.merchant_last_message"} <= fields(draft)
    assert only(candidates, A.DRAFT_CAMPAIGN).selected_offer_id == "o_mylari_001"


def test_active_planning_cites_veras_earlier_suggestion() -> None:
    candidates = generate_candidates(seed_context("trg_016_kids_yoga_program_drafting"))

    assert actions(candidates) == [A.DRAFT_ARTIFACT]
    assert "conversation_history.1.body" in fields(candidates[0])


def test_inferred_planning_intent_may_ask_first() -> None:
    payload = {"intent_topic": "corporate_bulk_thali_package"}
    parts = seed_context_parts("trg_013_corporate_thali_planning")
    parts["trigger"]["payload"] = payload
    parts["merchant"]["conversation_history"] = []
    candidates = generate_candidates(CandidateGenerationContext(**parts, now=SEED_NOW))

    assert A.ASK_MERCHANT in actions(candidates)
    assert only(candidates, A.DRAFT_ARTIFACT).conversation_relevance == 0.0


def test_planning_without_topic_or_request_is_restraint() -> None:
    parts = seed_context_parts("trg_016_kids_yoga_program_drafting")
    parts["trigger"]["payload"] = {}
    parts["merchant"]["conversation_history"] = []

    assert actions(generate_candidates(CandidateGenerationContext(**parts, now=SEED_NOW))) == [A.NO_ACTION]


def test_curious_ask_asks_when_nothing_is_pending() -> None:
    candidates = generate_candidates(seed_context("trg_008_curious_ask_studio11"))

    assert actions(candidates) == [A.ASK_MERCHANT]
    assert candidates[0].cta_type is CTAType.OPEN_ENDED


def _curious_ask_for(trigger_id_of_merchant: str) -> CandidateGenerationContext:
    parts = seed_context_parts(trigger_id_of_merchant)
    curious = seed_context_parts("trg_008_curious_ask_studio11")["trigger"]
    curious["merchant_id"] = parts["merchant"]["merchant_id"]
    parts["trigger"] = curious
    return CandidateGenerationContext(**parts, now=SEED_NOW)


def test_curious_ask_does_not_reask_while_merchant_awaits_an_answer() -> None:
    candidates = generate_candidates(_curious_ask_for("trg_012_milestone_mylari"))  # open "what would it look like"
    draft = only(candidates, A.DRAFT_ARTIFACT)

    assert A.ASK_MERCHANT not in actions(candidates)
    assert draft.conversation_relevance == 1.0
    assert {"conversation_history.0.body", "conversation_history.1.body"} <= fields(draft)


def test_open_request_is_answered_with_what_vera_offered() -> None:
    candidates = generate_candidates(_curious_ask_for("trg_001_research_digest_dentists"))  # "Want me to draft 3 posts" -> "Yes please"

    assert actions(candidates) == [A.DRAFT_POST]
    assert candidates[0].cta_type is CTAType.CONFIRMATION


def test_live_conversation_raises_conversation_relevance() -> None:
    ts = "2026-04-26T04:00:00Z"
    live = {
        "conversation_id": "c1",
        "created_at": ts,
        "updated_at": ts,
        "turns": [{"role": "merchant", "body": "Any update on the IPL match plan?", "sent_at": ts, "recorded_at": ts}],
    }
    parts = seed_context_parts("trg_010_ipl_match_delhi")
    parts["merchant"]["conversation_history"] = []
    without = generate_candidates(CandidateGenerationContext(**parts, now=SEED_NOW))
    with_live = generate_candidates(CandidateGenerationContext(**parts, conversation=live, now=SEED_NOW))

    assert all(c.conversation_relevance == 0.0 for c in without)
    assert all(c.conversation_relevance > 0.0 for c in with_live)


def test_dormant_merchant_gets_a_question_and_a_grounded_insight() -> None:
    candidates = generate_candidates(seed_context("trg_025_dormancy_glamour"))
    insight = only(candidates, A.SEND_INSIGHT)

    assert set(actions(candidates)) == {A.ASK_MERCHANT, A.SEND_INSIGHT}
    assert "performance.delta_7d.calls_pct" in fields(insight)
    assert "conversation_history.0.body" in fields(insight)


# --------------------------------------------------------------------------- #
# CUSTOMER_TIMING
# --------------------------------------------------------------------------- #


def test_recall_reminder_is_customer_scoped_with_real_slots_and_offer() -> None:
    candidates = generate_candidates(seed_context("trg_003_recall_due_priya"))
    reminder = only(candidates, A.SEND_CUSTOMER_REMINDER)
    draft = only(candidates, A.DRAFT_MESSAGE)

    assert (reminder.scope, reminder.send_as, reminder.customer_id) == (
        DecisionScope.CUSTOMER,
        SendAs.MERCHANT_ON_BEHALF,
        "c_001_priya_for_m001",
    )
    assert reminder.cta_type is CTAType.CONFIRMATION
    assert reminder.selected_offer_id == "o_meera_001"
    assert {"payload.available_slots", "payload.service_due", "offers.0.title"} <= fields(reminder)
    assert (draft.scope, draft.send_as, draft.customer_id) == (DecisionScope.MERCHANT, SendAs.VERA, "c_001_priya_for_m001")


@pytest.mark.parametrize(
    ("trigger_id", "action", "cta"),
    [
        ("trg_007_bridal_followup_kavya", A.SEND_CUSTOMER_FOLLOWUP, CTAType.YES_NO),
        ("trg_015_winback_rashmi", A.SEND_CUSTOMER_WINBACK, CTAType.YES_NO),
        ("trg_017_kids_yoga_trial_followup_karthik", A.SEND_CUSTOMER_FOLLOWUP, CTAType.CONFIRMATION),
        ("trg_019_chronic_refill_grandfather", A.SEND_CUSTOMER_REMINDER, CTAType.CONFIRMATION),
    ],
)
def test_customer_moment_actions(trigger_id: str, action: ActionType, cta: CTAType) -> None:
    candidate = only(generate_candidates(seed_context(trigger_id)), action)

    assert candidate.scope is DecisionScope.CUSTOMER
    assert candidate.send_as is SendAs.MERCHANT_ON_BEHALF
    assert candidate.cta_type is cta


def test_hard_lapse_also_proposes_retention_to_the_merchant() -> None:
    retention = only(generate_candidates(seed_context("trg_015_winback_rashmi")), A.RECOMMEND_RETENTION)

    assert retention.scope is DecisionScope.MERCHANT
    assert "customer_aggregate.monthly_churn_pct" in fields(retention)


def test_refill_deadline_creates_time_pressure() -> None:
    reminder = only(generate_candidates(seed_context("trg_019_chronic_refill_grandfather")), A.SEND_CUSTOMER_REMINDER)

    assert reminder.time_pressure >= 0.8
    assert "payload.molecule_list" in fields(reminder)


def test_placeholder_customer_trigger_falls_back_to_customer_facts() -> None:
    ctx = ctx_for("trg_003_recall_due_priya", kind="appointment_tomorrow", payload={"placeholder": True})
    candidates = generate_candidates(ctx)
    reminder = only(candidates, A.SEND_CUSTOMER_REMINDER)

    assert "relationship.last_visit" in fields(reminder)
    assert reminder.time_pressure == 1.0
    assert reminder.cta_type is CTAType.CONFIRMATION
    assert reminder.selected_offer_id is None


def test_customer_kind_without_customer_is_restraint_not_a_customer_send() -> None:
    parts = seed_context_parts("trg_003_recall_due_priya")
    parts["trigger"].update(customer_id=None, scope="merchant")
    parts["customer"] = None
    ctx = CandidateGenerationContext(**parts, now=SEED_NOW)
    candidates = generate_candidates(ctx)

    assert actions(candidates) == [A.NO_ACTION]
    assert candidates[0].scope is DecisionScope.MERCHANT


# --------------------------------------------------------------------------- #
# PERFORMANCE
# --------------------------------------------------------------------------- #


def test_dip_alerts_and_fixes_but_does_not_invent_a_campaign() -> None:
    candidates = generate_candidates(seed_context("trg_004_perf_dip_bharat"))  # m_002 has no offers

    assert set(actions(candidates)) == {A.SEND_ALERT, A.RECOMMEND_OPERATIONAL_FIX}
    assert "payload.delta_pct" in fields(only(candidates, A.SEND_ALERT))
    fix = only(candidates, A.RECOMMEND_OPERATIONAL_FIX)
    cited_signals = {e.value for e in fix.evidence if e.field.startswith("signals.")}
    assert cited_signals == {"unverified_gbp", "no_active_offers"}


def test_dip_campaign_only_promotes_existing_offers() -> None:
    offers = [{"id": "o_x", "title": "Dental Cleaning @ ₹299", "status": "active"}, {"id": "o_y", "title": "Old", "status": "expired"}]
    candidates = generate_candidates(ctx_for("trg_004_perf_dip_bharat", merchant={"offers": offers}))

    assert [c.selected_offer_id for c in candidates if c.action is A.DRAFT_CAMPAIGN] == ["o_x"]


def test_spike_is_never_treated_as_a_problem() -> None:
    candidates = generate_candidates(seed_context("trg_024_perf_spike_zen"))

    assert A.SEND_ALERT not in actions(candidates)
    assert A.RECOMMEND_OPERATIONAL_FIX not in actions(candidates)
    assert only(candidates, A.SEND_INSIGHT).cta_type is CTAType.NONE
    assert "payload.likely_driver" in fields(only(candidates, A.DRAFT_POST))


@pytest.mark.parametrize(("trigger_id", "delta"), [("trg_004_perf_dip_bharat", 0.2), ("trg_024_perf_spike_zen", -0.2)])
def test_contradicted_direction_is_restraint(trigger_id: str, delta: float) -> None:
    assert actions(generate_candidates(patch_payload(trigger_id, delta_pct=delta))) == [A.NO_ACTION]


def test_placeholder_dip_uses_merchant_performance() -> None:
    candidates = generate_candidates(ctx_for("trg_004_perf_dip_bharat", payload={"placeholder": True}))

    assert "performance.delta_7d.calls_pct" in fields(only(candidates, A.SEND_ALERT))


def test_seasonal_dip_is_reframed_not_alarmed() -> None:
    candidates = generate_candidates(seed_context("trg_014_seasonal_acquisition_dip_powerhouse"))

    assert set(actions(candidates)) == {A.SEND_INSIGHT, A.RECOMMEND_RETENTION, A.NO_ACTION}
    assert "customer_aggregate.total_active_members" in fields(only(candidates, A.RECOMMEND_RETENTION))
    assert "payload.is_expected_seasonal" in fields(only(candidates, A.NO_ACTION))


def test_unexpected_seasonal_dip_is_handled_as_a_dip() -> None:
    candidates = generate_candidates(patch_payload("trg_014_seasonal_acquisition_dip_powerhouse", is_expected_seasonal=False))

    assert A.SEND_ALERT in actions(candidates)


def test_imminent_review_milestone_proposes_review_requests() -> None:
    candidates = generate_candidates(seed_context("trg_012_milestone_mylari"))

    assert set(actions(candidates)) == {A.SEND_INSIGHT, A.DRAFT_MESSAGE}


def test_reached_milestone_proposes_a_post() -> None:
    candidates = generate_candidates(patch_payload("trg_012_milestone_mylari", value_now=150, is_imminent=False))

    assert set(actions(candidates)) == {A.SEND_INSIGHT, A.DRAFT_POST}


def test_milestone_without_numbers_is_restraint() -> None:
    assert actions(generate_candidates(ctx_for("trg_012_milestone_mylari", payload={"placeholder": True}))) == [A.NO_ACTION]


# --------------------------------------------------------------------------- #
# MARKET_OPPORTUNITY
# --------------------------------------------------------------------------- #


def test_weekend_ipl_match_argues_against_a_match_night_promo() -> None:
    candidates = generate_candidates(seed_context("trg_010_ipl_match_delhi"))
    insight = only(candidates, A.SEND_INSIGHT)
    campaign = only(candidates, A.DRAFT_CAMPAIGN)

    assert {"payload.is_weeknight", "digest.0.title"} <= fields(insight)
    assert campaign.selected_offer_id == "o_skpz_001"
    assert "customer_aggregate.delivery_orders_30d" in fields(campaign)
    assert "delivery" in campaign.objective and "instead of a match-night promotion" in campaign.objective


def test_weeknight_ipl_match_captures_demand() -> None:
    candidates = generate_candidates(patch_payload("trg_010_ipl_match_delhi", is_weeknight=True))

    assert only(candidates, A.SEND_INSIGHT).cta_type is CTAType.NONE
    assert only(candidates, A.DRAFT_CAMPAIGN).objective == "capture match-night demand with an offer the merchant already runs"


def test_ipl_match_in_another_city_is_restraint() -> None:
    assert actions(generate_candidates(patch_payload("trg_010_ipl_match_delhi", city="Mumbai"))) == [A.NO_ACTION]


def test_far_festival_includes_restraint_and_existing_offers_only() -> None:
    candidates = generate_candidates(seed_context("trg_006_festival_diwali"))

    assert set(actions(candidates)) == {A.ASK_MERCHANT, A.DRAFT_CAMPAIGN, A.NO_ACTION}
    assert sorted(c.selected_offer_id for c in candidates if c.action is A.DRAFT_CAMPAIGN) == ["o_studio11_001", "o_studio11_002"]


def test_festival_outside_category_is_restraint() -> None:
    assert actions(generate_candidates(patch_payload("trg_006_festival_diwali", category_relevance=["gyms"]))) == [A.NO_ACTION]


def test_unnamed_festival_is_restraint() -> None:
    assert actions(generate_candidates(ctx_for("trg_006_festival_diwali", payload={"placeholder": True}))) == [A.NO_ACTION]


def test_research_digest_cites_item_and_matching_cohort() -> None:
    insight = only(generate_candidates(seed_context("trg_001_research_digest_dentists")), A.SEND_INSIGHT)

    assert {"digest.0.title", "digest.0.source", "signals.2", "customer_aggregate.high_risk_adult_count"} <= fields(insight)


def test_research_digest_release_alias_with_inline_item() -> None:
    top_item = {"title": "3-month fluoride recall outperforms 6-month", "source": "JIDA Oct 2026", "actionable": "Offer 3-month recall"}
    ctx = ctx_for("trg_001_research_digest_dentists", kind="research_digest_release", payload={"top_item": top_item})
    candidates = generate_candidates(ctx)

    assert set(actions(candidates)) == {A.SEND_INSIGHT, A.DRAFT_ARTIFACT}
    assert "payload.top_item.title" in fields(only(candidates, A.SEND_INSIGHT))


def test_category_seasonal_recommends_shelf_action() -> None:
    candidates = generate_candidates(seed_context("trg_020_summer_demand_shift"))

    assert set(actions(candidates)) == {A.SEND_INSIGHT, A.DRAFT_POST, A.RECOMMEND_OPERATIONAL_FIX}
    assert "payload.trends" in fields(only(candidates, A.SEND_INSIGHT))


def test_cde_opportunity_cites_the_session() -> None:
    insight = only(generate_candidates(seed_context("trg_022_cde_webinar_dentists")), A.SEND_INSIGHT)

    assert {"digest.2.title", "digest.2.date", "payload.credits"} <= fields(insight)


# --------------------------------------------------------------------------- #
# COMPETITIVE
# --------------------------------------------------------------------------- #


def test_competitor_facts_come_only_from_the_trigger() -> None:
    candidates = generate_candidates(seed_context("trg_023_competitor_opened_dentist"))

    assert set(actions(candidates)) == {A.SEND_INSIGHT, A.ASK_MERCHANT, A.DRAFT_LISTING, A.DRAFT_CAMPAIGN}
    for candidate in candidates:
        competitor_facts = {e.field for e in candidate.evidence if e.source is EvidenceSource.TRIGGER and e.field != "kind"}
        assert competitor_facts <= {"payload.competitor_name", "payload.distance_km", "payload.their_offer", "payload.opened_date"}
    assert only(candidates, A.DRAFT_CAMPAIGN).selected_offer_id == "o_meera_001"


def test_unnamed_competitor_is_never_discussed() -> None:
    candidates = generate_candidates(ctx_for("trg_023_competitor_opened_dentist", payload={"placeholder": True}))

    assert set(actions(candidates)) == {A.NO_ACTION, A.DRAFT_LISTING}
    assert "review_themes.1.theme" in fields(only(candidates, A.DRAFT_LISTING))


# --------------------------------------------------------------------------- #
# OPERATIONS
# --------------------------------------------------------------------------- #


def test_renewal_inside_notice_window_alerts() -> None:
    candidates = generate_candidates(seed_context("trg_005_renewal_due_bharat"))

    assert actions(candidates) == [A.SEND_ALERT]  # m_002's numbers are all down: no value recap
    assert {"payload.days_remaining", "payload.renewal_amount"} <= fields(candidates[0])


@pytest.mark.parametrize(
    ("payload", "merchant"),
    [({"days_remaining": 120}, None), ({"placeholder": True}, {"subscription": {"status": "expired", "days_remaining": 0}})],
)
def test_renewal_far_off_or_expired_is_restraint(payload: dict, merchant: dict | None) -> None:
    assert actions(generate_candidates(ctx_for("trg_005_renewal_due_bharat", payload=payload, merchant=merchant))) == [A.NO_ACTION]


def test_gbp_unverified_recommends_verification() -> None:
    candidates = generate_candidates(seed_context("trg_021_unverified_gbp_sunrise"))

    assert set(actions(candidates)) == {A.RECOMMEND_OPERATIONAL_FIX, A.ASK_MERCHANT}


def test_verified_listing_contradicts_gbp_trigger() -> None:
    parts = seed_context_parts("trg_021_unverified_gbp_sunrise")
    parts["merchant"]["identity"]["verified"] = True

    assert actions(generate_candidates(CandidateGenerationContext(**parts, now=SEED_NOW))) == [A.NO_ACTION]


def test_merchant_winback_shows_what_slipped() -> None:
    candidates = generate_candidates(seed_context("trg_009_winback_glamour"))

    assert set(actions(candidates)) == {A.ASK_MERCHANT, A.SEND_INSIGHT}
    assert {"payload.days_since_expiry", "payload.lapsed_customers_added_since_expiry"} <= fields(only(candidates, A.SEND_INSIGHT))


def test_winback_for_active_subscription_is_restraint() -> None:
    ctx = ctx_for("trg_009_winback_glamour", merchant={"subscription": {"status": "active", "days_remaining": 100}})

    assert actions(generate_candidates(ctx)) == [A.NO_ACTION]


def test_negative_review_theme_proposes_fix_and_responses() -> None:
    candidates = generate_candidates(seed_context("trg_011_review_theme_late_delivery"))

    assert set(actions(candidates)) == {A.RECOMMEND_OPERATIONAL_FIX, A.DRAFT_MESSAGE}
    assert "review_themes.0.sentiment" in fields(only(candidates, A.RECOMMEND_OPERATIONAL_FIX))


def test_positive_review_theme_is_showcased() -> None:
    candidates = generate_candidates(patch_payload("trg_011_review_theme_late_delivery", theme="pizza_quality"))

    assert set(actions(candidates)) == {A.SEND_INSIGHT, A.DRAFT_POST}


def test_placeholder_review_theme_uses_merchant_themes() -> None:
    candidates = generate_candidates(ctx_for("trg_011_review_theme_late_delivery", payload={"placeholder": True}))

    assert "review_themes.0.theme" in fields(only(candidates, A.RECOMMEND_OPERATIONAL_FIX))


# --------------------------------------------------------------------------- #
# Generator plumbing
# --------------------------------------------------------------------------- #


def test_archetype_generator_ignores_kinds_it_does_not_own() -> None:
    generator = ArchetypeGenerator(TriggerArchetype.COMPETITIVE, {})

    assert generator.generate(seed_context("trg_023_competitor_opened_dentist")) == []
    assert generator.kinds == frozenset()
