"""Phase 3D tick-wording fixes, each tied to an observed gap in the canonical evaluation.

1. Customer reminders say what their trigger is about: an appointment reminder
   confirms the appointment and a refill reminder arranges the refill, instead of
   "book your next visit" (the plan objective and the body must agree).
2. The winback question uses the category's wording ("session" is gym vocabulary).
3. The merchant's own figure is not crowded out of the lead by the length budget.

Engine-level (no FastAPI). Decisions are untouched; see test_composer_golden for the
canonical decision check.
"""

import json
import re
from typing import Any

import pytest

from app.engine.actions import ActionType, DecisionScope
from app.engine.candidates import CandidateGenerationContext
from app.engine.composer import HIDDEN_LABELS, WINBACK_QUESTIONS, compose, label_of
from app.engine.evidence import EvidenceSource
from app.engine.planner import plan_tick
from app.engine.plans import DecisionPlan
from app.models.enums import TurnRole
from tests.conftest import SEED_NOW, expanded_context, requires_dataset, seed_context_parts
from tests.test_composer import all_candidate_plans, context_text, last_sentence, seed_plan, select
from tests.test_planner import push, seeded
from tests.test_reply_composer import judge_reply
from tests.test_reply_dataset import REPLY_AT

pytestmark = requires_dataset

A = ActionType
RECALL = "trg_003_recall_due_priya"
KIDS_TRIAL = "trg_017_kids_yoga_trial_followup_karthik"
WINBACK = "trg_015_winback_rashmi"
IPL = "trg_010_ipl_match_delhi"
SUPPLY = "trg_018_supply_atorvastatin_recall"
DIGEST = "trg_001_research_digest_dentists"
APPOINTMENT_PLACEHOLDER = "trg_076_appointment_tomorrow_m_019_karim_salon_lu"
REFILL_PLACEHOLDER = "trg_081_chronic_refill_due_m_011_dr_sameer_dent"
RECALL_PLACEHOLDER = "trg_066_recall_due_m_008_zenyoga_gym_ch"
LAPSED_DENTIST = "trg_071_customer_lapsed_soft_m_014_dr_asha_dentis"
LAPSED_PHARMACY = "trg_072_customer_lapsed_soft_m_049_komal_pharmaci"

MOMENT_WORDS = {
    "recall_due": ("due", "visit"),
    "appointment_tomorrow": ("appointment",),
    "chronic_refill_due": ("refill", "run out"),
    "customer_lapsed_soft": ("last visit",),
    "customer_lapsed_hard": ("last visit",),
    "trial_followup": ("trial",),
    "wedding_package_followup": ("wedding",),
}


def appointment_trigger(customer_id: str, merchant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "trg_appt", "kind": "appointment_tomorrow", "scope": "customer", "source": "internal",
        "merchant_id": merchant_id, "customer_id": customer_id, "payload": payload, "urgency": 3,
        "suppression_key": f"appointment:{customer_id}:2026-04-27", "expires_at": "2026-04-28T00:00:00+05:30",
    }


def appointment_plan(base_trigger: str, payload: dict[str, Any]) -> tuple[DecisionPlan, CandidateGenerationContext]:
    parts = seed_context_parts(base_trigger)
    parts["trigger"] = appointment_trigger(parts["customer"]["customer_id"], parts["merchant"]["merchant_id"], payload)
    ctx = CandidateGenerationContext(**parts, now=SEED_NOW)
    return select(ctx), ctx


def expanded_plan(data: dict[str, Any], trigger_id: str) -> tuple[DecisionPlan, CandidateGenerationContext]:
    ctx = expanded_context(data, trigger_id)
    return select(ctx), ctx


def assert_grounded(ctx: CandidateGenerationContext, message) -> None:
    for ref in message.facts_used:
        source, _, path = ref.partition(":")
        assert ctx.value(EvidenceSource(source), path) is not None, ref
    text = context_text(ctx)
    for number in re.findall(r"\d+", message.body):
        assert number in text or str(int(number)) in text, (number, message.body)


def merchant_number_refs(plan: DecisionPlan, ctx: CandidateGenerationContext) -> set[str]:
    return {
        f"{e.source}:{e.field}" for e in plan.evidence
        if e.source is EvidenceSource.MERCHANT and isinstance(e.value, int | float) and not isinstance(e.value, bool)
        and ctx.value(EvidenceSource.MERCHANT, e.field) == e.value and label_of(e) not in HIDDEN_LABELS
    }


# --------------------------------------------------------------------------- #
# 1. Customer reminders follow their trigger kind
# --------------------------------------------------------------------------- #


def test_appointment_reminder_without_a_time_is_about_the_appointment(expanded: dict[str, Any]) -> None:
    plan, ctx = expanded_plan(expanded, APPOINTMENT_PLACEHOLDER)
    message = compose(plan, ctx)

    assert plan.objective == "confirm the customer's appointment tomorrow"
    assert message.body == (
        "Hi Aditya, Karim's Salon here. This is a reminder about your appointment tomorrow. "
        "Your last visit with us was on 1 Apr 2026. Reply CONFIRM to keep your appointment."
    )
    assert {"trigger:kind", "customer:relationship.last_visit"} <= set(message.facts_used)
    assert_grounded(ctx, message)


def test_appointment_reminder_with_a_booked_time_states_it() -> None:
    plan, ctx = appointment_plan(RECALL, {"appointment_iso": "2026-04-27T18:00:00+05:30", "service": "cleaning"})
    message = compose(plan, ctx)

    assert plan.action is A.SEND_CUSTOMER_REMINDER
    assert "Aapka cleaning appointment 27 Apr 2026, 6pm ko hai." in message.body  # Priya prefers hi-en mix
    assert last_sentence(message.body) == "Reply CONFIRM to keep your appointment."
    assert {"trigger:payload.appointment_iso", "trigger:payload.service"} <= set(message.facts_used)
    assert "last visit with us" not in message.body
    assert_grounded(ctx, message)


def test_appointment_reminder_for_a_child_names_the_child() -> None:
    plan, ctx = appointment_plan(KIDS_TRIAL, {})
    message = compose(plan, ctx)

    assert message.body.startswith("Hi Sumitra, Zen Yoga Studio here. This is a reminder about Karthik's appointment tomorrow.")


def test_refill_reminder_without_a_medicine_list_still_arranges_the_refill(expanded: dict[str, Any]) -> None:
    plan, ctx = expanded_plan(expanded, REFILL_PLACEHOLDER)
    message = compose(plan, ctx)

    assert plan.objective == "refill the customer's chronic medication before stock runs out"
    assert message.body == (
        "Hi Vivaan, Bright Smile Dental here. Your refill is due. "
        "Your last visit with us was on 1 Apr 2026. Reply CONFIRM to arrange your refill."
    )
    assert_grounded(ctx, message)


def test_other_reminders_keep_their_wording(expanded: dict[str, Any]) -> None:
    recall = compose(*expanded_plan(expanded, RECALL_PLACEHOLDER))
    assert recall.body == "Hi Diya, Zen Yoga Studio here. Your last visit with us was on 1 Apr 2026. Want us to book your next visit?"
    assert compose(*seed_plan(RECALL)).body.endswith("Reply CONFIRM to book Wed 5 Nov, 6pm.")


def test_a_yes_to_the_appointment_reminder_keeps_the_same_offer() -> None:
    state = seeded()
    push(state, "trigger", "trg_appt", appointment_trigger("c_001_priya_for_m001", "m_001_drmeera_dentist_delhi",
                                                           {"appointment_iso": "2026-04-27T18:00:00+05:30"}))
    (action,) = plan_tick(state, now=SEED_NOW, available_triggers=["trg_appt"]).actions
    assert action.body.endswith("Reply CONFIRM to keep your appointment.")

    decision = judge_reply(state, "yes", action.conversation_id, customer_id="c_001_priya_for_m001", from_role="customer")
    assert decision.response.body.endswith("Reply CONFIRM and we'll keep your appointment, or STOP to end here.")
    assert state.conversation_store.get(action.conversation_id).turns[-1].role is TurnRole.VERA


# --------------------------------------------------------------------------- #
# 2. The winback question uses the category's wording
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("trigger_id", "question"), [
    (LAPSED_DENTIST, "Want to book your next visit?"),
    (LAPSED_PHARMACY, "Want us to help with your next order?"),
])
def test_winback_question_matches_the_category(expanded: dict[str, Any], trigger_id: str, question: str) -> None:
    plan, ctx = expanded_plan(expanded, trigger_id)
    message = compose(plan, ctx)

    assert plan.action is A.SEND_CUSTOMER_WINBACK
    assert last_sentence(message.body) == question and message.body.count("?") == 1
    assert_grounded(ctx, message)


def test_gym_winback_keeps_its_session_wording() -> None:
    assert last_sentence(compose(*seed_plan(WINBACK)).body) == WINBACK_QUESTIONS["gyms"] == "Want to book a session?"


def test_session_wording_stays_with_gyms(expanded: dict[str, Any]) -> None:
    for plan, ctx in all_candidate_plans(expanded):
        if plan.scope is DecisionScope.CUSTOMER and ctx.category.get("slug") != "gyms":
            assert not re.search(r"\bsession\b", compose(plan, ctx).body), plan.trigger_id


# --------------------------------------------------------------------------- #
# 3. The merchant's own figure survives the lead budget
# --------------------------------------------------------------------------- #


def test_ipl_message_keeps_the_merchants_delivery_orders() -> None:
    plan, ctx = seed_plan(IPL)
    message = compose(plan, ctx)

    assert "You've had 180 delivery orders in the last 30 days." in message.body
    assert "merchant:customer_aggregate.delivery_orders_30d" in message.facts_used
    assert message.body.index("180 delivery orders") < message.body.index("Source: ")
    assert last_sentence(message.body) == "Want me to draft it?" and message.body.count("?") == 1
    assert_grounded(ctx, message)


def test_supply_alert_keeps_the_chronic_rx_count() -> None:
    plan, ctx = seed_plan(SUPPLY)
    message = compose(plan, ctx)

    assert "You have 240 chronic-Rx customers on record." in message.body
    assert "merchant:customer_aggregate.chronic_rx_count" in message.facts_used
    assert_grounded(ctx, message)


def test_no_extra_figure_when_the_lead_already_has_one() -> None:
    plan, ctx = seed_plan(DIGEST)
    message = compose(plan, ctx)

    assert "You have 124 matching patients on record." in message.body
    assert len(merchant_number_refs(plan, ctx) & set(message.facts_used)) == 1


def test_every_merchant_message_with_its_own_figure_renders_one(expanded: dict[str, Any]) -> None:
    for plan, ctx in all_candidate_plans(expanded):
        if plan.scope is DecisionScope.CUSTOMER:
            continue
        message = compose(plan, ctx)
        if merchant_number_refs(plan, ctx):
            assert merchant_number_refs(plan, ctx) & set(message.facts_used), (plan.trigger_id, plan.action.value, message.body)
        assert message.body.count("?") <= 1


# --------------------------------------------------------------------------- #
# Canonical pairs
# --------------------------------------------------------------------------- #


def test_canonical_bodies_name_their_moment_and_use_category_wording(expanded: dict[str, Any]) -> None:
    checked = 0
    for pair in expanded["pairs"]:
        plan, ctx = expanded_plan(expanded, pair["trigger_id"])
        if plan.is_no_action:
            continue
        message = compose(plan, ctx)
        assert_grounded(ctx, message)
        if plan.scope is DecisionScope.CUSTOMER:
            words = MOMENT_WORDS[ctx.canonical_kind]
            assert any(w in message.body.lower() for w in words), (pair["test_id"], message.body)
            if ctx.category.get("slug") != "gyms":
                assert "session" not in message.body, pair["test_id"]
            checked += 1
    assert checked == 9


def test_changes_are_wording_only(expanded: dict[str, Any]) -> None:
    """Composition never feeds back into the plan: the same plan composes identically twice and is not mutated."""
    for pair in expanded["pairs"]:
        plan, ctx = expanded_plan(expanded, pair["trigger_id"])
        if plan.is_no_action:
            continue
        before = json.dumps(plan.model_dump(mode="json"), sort_keys=True)
        assert compose(plan, ctx) == compose(plan, ctx)
        assert json.dumps(plan.model_dump(mode="json"), sort_keys=True) == before
