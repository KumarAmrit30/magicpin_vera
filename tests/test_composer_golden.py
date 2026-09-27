"""Phase 3 golden fixtures and tick integration.

Golden cases come from the seed dataset triggers behind the case studies. They
pin the decision-to-message contract (action, template, CTA, voice, grounded
facts, body shape), not exact wording: each case lists the facts that must
appear, the opening and the closing CTA sentence.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.engine.actions import CTAType, DecisionScope
from app.engine.composer import WIRE_CTA, compose
from app.engine.planner import TriggerOutcome, load_context, plan_tick
from app.models.enums import CtaType, SendAs, TurnRole
from app.state.container import StateContainer
from tests.conftest import SEED_NOW, load_seed_dataset, requires_dataset
from tests.test_composer import last_sentence, seed_plan
from tests.test_planner import seeded, tick
from tests.test_planner_dataset import loaded, phase_2d

pytestmark = requires_dataset

GOLDEN: list[dict[str, Any]] = [
    {
        "trigger": "trg_001_research_digest_dentists", "action": "draft_artifact", "cta": "binary_yes_no", "send_as": "vera",
        "starts": "Dr. Meera \u2014 ", "contains": ["3-month fluoride varnish recall", "124", "Source: JIDA Oct 2026, p.14."],
        "last": "Want me to draft it?",
        "facts": ["category:digest.0.title", "category:digest.0.source", "merchant:customer_aggregate.high_risk_adult_count"],
    },
    {
        "trigger": "trg_002_compliance_dci_radiograph", "action": "send_alert", "cta": "binary_yes_no", "send_as": "vera",
        "starts": "Dr. Meera \u2014 ", "contains": ["1.5 mSv to 1.0 mSv", "Deadline: 15 Dec 2026.", "Dental Council of India"],
        "last": "Want me to share the next steps?",
        "facts": ["trigger:payload.deadline_iso", "category:digest.1.source"],
    },
    {
        "trigger": "trg_003_recall_due_priya", "action": "send_customer_reminder", "cta": "binary_confirm_cancel",
        "send_as": "merchant_on_behalf", "starts": "Namaste Priya, Dr. Meera's Dental Clinic here.",
        "contains": ["6 month cleaning", "12 Nov 2026", "Dental Cleaning @ \u20b9299", "Wed 5 Nov, 6pm"],
        "last": "Reply CONFIRM to book Wed 5 Nov, 6pm.",
        "facts": ["customer:identity.name", "trigger:payload.available_slots", "merchant:offers.0.title"],
    },
    {
        "trigger": "trg_004_perf_dip_bharat", "action": "recommend_operational_fix", "cta": "binary_yes_no", "send_as": "vera",
        "starts": "Dr. Bharat \u2014 ", "contains": ["Your calls are down 50%.", "verifying your Google profile"],
        "last": "Want me to walk you through it?",
        "facts": ["trigger:payload.delta_pct"],
    },
    {
        "trigger": "trg_007_bridal_followup_kavya", "action": "send_customer_followup", "cta": "binary_yes_no",
        "send_as": "merchant_on_behalf", "starts": "Hi Kavya, Studio11 Family Salon here.",
        "contains": ["196 days", "8 Nov 2026"], "last": "Want us to book your next visit?",
        "facts": ["trigger:payload.wedding_date", "trigger:payload.days_to_wedding"],
    },
    {
        "trigger": "trg_008_curious_ask_studio11", "action": "ask_merchant", "cta": "open_ended", "send_as": "vera",
        "starts": "Hi Lakshmi \u2014 ", "contains": [], "last": "What service have customers asked for most this week?",
        "facts": ["trigger:payload.ask_template"],
    },
    {
        "trigger": "trg_010_ipl_match_delhi", "action": "draft_campaign", "cta": "binary_yes_no", "send_as": "vera",
        "starts": "Hi Suresh \u2014 ", "contains": ["DC vs MI", "Buy 1 Pizza Get 1 Free", "Saturdays underperformed"],
        "last": "Want me to draft it?",
        "facts": ["trigger:payload.match", "merchant:offers.0.title"],
    },
    {
        "trigger": "trg_011_review_theme_late_delivery", "action": "draft_message", "cta": "binary_yes_no", "send_as": "vera",
        "starts": "Hi Suresh \u2014 ", "contains": ["delivery late", "\u201ctook 50 mins for a 15 min ride\u201d", "4 times"],
        "last": "Want me to draft it?", "facts": ["trigger:payload.common_quote", "trigger:payload.occurrences_30d"],
    },
    {
        "trigger": "trg_014_seasonal_acquisition_dip_powerhouse", "action": "recommend_retention", "cta": "binary_yes_no",
        "send_as": "vera", "starts": "Hi Karthik \u2014 ", "contains": ["down 30%", "245", "expected seasonal pattern"],
        "last": "Want me to draft a retention plan?",
        "facts": ["merchant:customer_aggregate.total_active_members"],
    },
    {
        "trigger": "trg_015_winback_rashmi", "action": "send_customer_winback", "cta": "binary_yes_no",
        "send_as": "merchant_on_behalf", "starts": "Hi Rashmi, PowerHouse Fitness here.",
        "contains": ["57 days", "weight loss"], "last": "Want to book a session?",
        "facts": ["trigger:payload.days_since_last_visit"],
    },
    {
        "trigger": "trg_016_kids_yoga_program_drafting", "action": "draft_artifact", "cta": "binary_confirm_cancel",
        "send_as": "vera", "starts": "Hi Padma \u2014 ", "contains": ["kids yoga summer camp"],
        "last": "Reply CONFIRM and I'll draft it.", "facts": ["trigger:payload.intent_topic"],
    },
    {
        "trigger": "trg_018_supply_atorvastatin_recall", "action": "draft_message", "cta": "binary_yes_no", "send_as": "vera",
        "starts": "Hi Ramesh \u2014 ", "contains": ["AT2024-1102", "atorvastatin", "MfrZ"], "last": "Want me to draft it?",
        "facts": ["trigger:payload.affected_batches", "trigger:payload.manufacturer"],
    },
    {
        "trigger": "trg_019_chronic_refill_grandfather", "action": "send_customer_reminder", "cta": "binary_confirm_cancel",
        "send_as": "merchant_on_behalf", "starts": "Namaste Mr. Sharma, Apollo Health Plus Pharmacy here.",
        "contains": ["metformin", "28 Apr 2026", "saved"], "last": "Reply CONFIRM to arrange your refill.",
        "facts": ["trigger:payload.molecule_list", "trigger:payload.stock_runs_out_iso"],
    },
    {
        "trigger": "trg_023_competitor_opened_dentist", "action": "draft_listing", "cta": "binary_yes_no", "send_as": "vera",
        "starts": "Dr. Meera \u2014 ", "contains": ["Smile Studio", "1.3 km", "doctor manner"], "last": "Want me to draft it?",
        "facts": ["trigger:payload.competitor_name", "trigger:payload.distance_km"],
    },
]


@pytest.mark.parametrize("case", GOLDEN, ids=[c["trigger"] for c in GOLDEN])
def test_golden_message_contract(case: dict[str, Any]) -> None:
    plan, ctx = seed_plan(case["trigger"])
    message = compose(plan, ctx)

    assert plan.action.value == case["action"]
    assert message.template_name == f"vera_{case['action']}_v1"
    assert message.cta.value == case["cta"]
    assert message.send_as.value == case["send_as"]
    assert message.body.startswith(case["starts"]), message.body
    assert all(fact in message.body for fact in case["contains"]), message.body
    assert message.body.endswith(f" {case['last']}") and case["last"] in last_sentence(message.body), message.body
    assert set(case["facts"]) <= set(message.facts_used), message.facts_used


def test_golden_set_spans_every_archetype_and_both_scopes() -> None:
    plans = [seed_plan(c["trigger"])[0] for c in GOLDEN]
    assert {p.archetype.value for p in plans} == {
        "safety_compliance", "active_intent", "customer_timing", "performance", "market_opportunity", "competitive", "operations",
    }
    assert {p.scope for p in plans} == set(DecisionScope)


# --------------------------------------------------------------------------- #
# D. /tick emits composed messages
# --------------------------------------------------------------------------- #


def test_tick_actions_carry_the_composed_message() -> None:
    state = seeded()
    triggers = [t for t, _ in load_seed_dataset()["trigger"]]
    result = tick(state, *triggers)

    assert result.actions
    decisions = {d.trigger_id: d for d in result.decisions}
    for action in result.actions:
        plan = decisions[action.trigger_id].plan
        message = compose(plan, load_context(state.context_store, action.trigger_id, SEED_NOW))
        assert (action.body, action.template_name, action.template_params, action.cta, action.send_as) == (
            message.body, message.template_name, list(message.template_params), message.cta, message.send_as,
        )
        assert "[uncomposed" not in action.body
        (turn,) = state.conversation_store.get(action.conversation_id).turns
        assert (turn.role, turn.body) == (TurnRole.VERA, action.body)


def test_judge_style_batches_emit_only_composed_messages(expanded: dict[str, Any]) -> None:
    state = loaded(expanded)
    trigger_ids = sorted(expanded["triggers"])
    emitted = []
    for start in range(0, len(trigger_ids), 5):
        emitted += plan_tick(state, now=SEED_NOW, available_triggers=trigger_ids[start:start + 5]).actions

    assert emitted
    for action in emitted:
        assert "[uncomposed" not in action.body and action.template_name.endswith("_v1")
        if action.cta in {CtaType.BINARY_YES_NO, CtaType.OPEN_ENDED}:
            assert action.body.count("?") == 1 and action.body.endswith("?")
        else:
            assert "?" not in action.body


# --------------------------------------------------------------------------- #
# M. The 30 canonical decisions are unchanged by composition
# --------------------------------------------------------------------------- #


def test_canonical_decisions_and_action_fields_are_unchanged(expanded: dict[str, Any]) -> None:
    emitted = 0
    for pair in expanded["pairs"]:
        trigger_id = pair["trigger_id"]
        result = plan_tick(loaded(expanded), now=SEED_NOW, available_triggers=[trigger_id])
        (decision,) = result.decisions
        expected = phase_2d(expanded, trigger_id)

        assert decision.plan == expected, pair["test_id"]
        assert (decision.plan.action, decision.plan.selected_offer_id, decision.plan.scope, decision.plan.suppression_key,
                decision.plan.priority_score, decision.plan.plan_id) == (
            expected.action, expected.selected_offer_id, expected.scope, expected.suppression_key,
            expected.priority_score, expected.plan_id,
        ), pair["test_id"]
        if expected.is_no_action:
            assert (decision.outcome, result.actions) == (TriggerOutcome.NO_ACTION, ()), pair["test_id"]
            continue
        emitted += 1
        (action,) = result.actions
        assert (action.trigger_id, action.merchant_id, action.customer_id, action.send_as, action.suppression_key, action.cta) == (
            expected.trigger_id, expected.merchant_id, expected.customer_id, expected.send_as, expected.suppression_key,
            WIRE_CTA[expected.cta_type],
        ), pair["test_id"]
        assert action.rationale == (
            f"{expected.action.value} ({expected.scope.value}) for {expected.trigger_id}: {expected.objective}. "
            f"priority={expected.priority_score} confidence={expected.confidence} plan_id={expected.plan_id}"
        ), pair["test_id"]
    assert (len(expanded["pairs"]), emitted) == (30, 27)


def test_customer_plans_are_sent_on_behalf_and_merchant_plans_by_vera(expanded: dict[str, Any]) -> None:
    state = loaded(expanded)
    for trigger_id in sorted(expanded["triggers"])[:40]:
        for action in plan_tick(state, now=SEED_NOW, available_triggers=[trigger_id]).actions:
            expected = SendAs.MERCHANT_ON_BEHALF if action.customer_id else SendAs.VERA
            assert action.send_as is expected


# --------------------------------------------------------------------------- #
# API contract
# --------------------------------------------------------------------------- #


def test_tick_api_returns_composed_bodies(client: TestClient, state: StateContainer) -> None:
    from tests.test_api_contract import push_seed

    push_seed(client)
    response = client.post(
        "/v1/tick",
        json={"now": "2026-04-26T04:30:00Z", "available_triggers": ["trg_001_research_digest_dentists", "trg_003_recall_due_priya"]},
    )

    assert response.status_code == 200
    actions = {a["trigger_id"]: a for a in response.json()["actions"]}
    digest, recall = actions["trg_001_research_digest_dentists"], actions["trg_003_recall_due_priya"]
    assert digest["body"].startswith("Dr. Meera \u2014 ") and digest["cta"] == "binary_yes_no"
    assert digest["template_name"] == "vera_draft_artifact_v1"
    assert recall["body"].endswith("Reply CONFIRM to book Wed 5 Nov, 6pm.") and recall["cta"] == "binary_confirm_cancel"
    assert all(isinstance(p, str) for a in actions.values() for p in a["template_params"])
    assert all("[uncomposed" not in a["body"] for a in actions.values())


def test_no_action_cta_type_never_reaches_the_wire() -> None:
    assert WIRE_CTA[CTAType.NONE] is CtaType.NONE
