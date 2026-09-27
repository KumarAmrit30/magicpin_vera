"""Phase 3 composer: DecisionPlan + context -> grounded, deterministic message.

Engine-level (no FastAPI). Dataset-wide checks compose every eligible candidate
of every expanded-dataset trigger, so each action type and CTA shape is
exercised with real contexts.
"""

import json
import re
from functools import cache
from typing import Any

import pytest

from app.engine.actions import ActionType, CTAType, DecisionScope
from app.engine.candidates import CandidateGenerationContext, generate_candidates
from app.engine.composer import (
    TEMPLATE_VERSION,
    WIRE_CTA,
    ComposedMessage,
    CompositionError,
    compose,
    humanize,
    reads_as,
)
from app.engine.eligibility import evaluate_candidates
from app.engine.evidence import EvidenceSource
from app.engine.plans import DecisionPlan
from app.engine.selection import plan_from_candidate, select_decision
from app.models.enums import CtaType, SendAs
from app.state.suppression_store import SuppressionStore
from tests.conftest import SEED_NOW, expanded_context, requires_dataset, seed_context, seed_context_parts

pytestmark = requires_dataset

A = ActionType
DIGEST = "trg_001_research_digest_dentists"
COMPLIANCE = "trg_002_compliance_dci_radiograph"
RECALL = "trg_003_recall_due_priya"
PERF_DIP = "trg_004_perf_dip_bharat"
CURIOUS = "trg_008_curious_ask_studio11"
IPL = "trg_010_ipl_match_delhi"
KIDS_TRIAL = "trg_017_kids_yoga_trial_followup_karthik"
REFILL = "trg_019_chronic_refill_grandfather"
WINBACK = "trg_015_winback_rashmi"

INTERNAL_MARKERS = ("[uncomposed", "{{", "{}", "None", "priority", "confidence", "plan_id", "score", "suppress", "trigger")
CUSTOMER_FORBIDDEN = ("Vera", "customer state", "lapsed", "cohort", "signal", "merchant")


def select(ctx: CandidateGenerationContext) -> DecisionPlan:
    return select_decision(ctx, evaluate_candidates(generate_candidates(ctx), ctx, SuppressionStore(), now=SEED_NOW))


def seed_plan(trigger_id: str, **overrides: Any) -> tuple[DecisionPlan, CandidateGenerationContext]:
    ctx = seed_context(trigger_id, **overrides)
    return select(ctx), ctx


def all_candidate_plans(data: dict[str, Any]) -> list[tuple[DecisionPlan, CandidateGenerationContext]]:
    """Every eligible candidate of every expanded trigger, realized as a plan."""
    out = []
    for trigger_id in sorted(data["triggers"]):
        ctx = expanded_context(data, trigger_id)
        for result in evaluate_candidates(generate_candidates(ctx), ctx, SuppressionStore(), now=SEED_NOW):
            if result.eligible and result.candidate.action is not A.NO_ACTION:
                out.append((plan_from_candidate(result.candidate, priority_score=50.0, confidence=0.5), ctx))
    return out


@pytest.fixture(scope="module")
def composed(expanded: dict[str, Any]) -> list[tuple[DecisionPlan, CandidateGenerationContext, ComposedMessage]]:
    return [(plan, ctx, compose(plan, ctx)) for plan, ctx in all_candidate_plans(expanded)]


def context_text(ctx: CandidateGenerationContext) -> str:
    return json.dumps([ctx.category, ctx.merchant, ctx.trigger, ctx.customer], ensure_ascii=False)


def last_sentence(body: str) -> str:
    return re.split(r"(?<=[.!?])\s+", body.strip())[-1]


# --------------------------------------------------------------------------- #
# A. Every emitted ActionType has a composition path
# --------------------------------------------------------------------------- #

EMITTED_ACTIONS = set(A) - {A.NO_ACTION}


def test_every_action_type_is_composed_from_real_candidates(composed) -> None:
    seen = {plan.action for plan, _, _ in composed}
    assert seen == EMITTED_ACTIONS
    assert all(m.body for _, _, m in composed)


def test_every_action_and_cta_shape_composes_without_facts() -> None:
    """With no evidence at all, every action/CTA combination still yields a safe, fact-free message."""
    ctx = seed_context(DIGEST)
    for action in EMITTED_ACTIONS - {A.SEND_CUSTOMER_REMINDER, A.SEND_CUSTOMER_WINBACK, A.SEND_CUSTOMER_FOLLOWUP}:
        for cta in CTAType:
            plan = DecisionPlan(
                trigger_id=DIGEST, archetype="market_opportunity", scope="merchant", merchant_id=ctx.merchant_id,
                objective="x", action=action, cta_type=cta, send_as="vera", suppression_key="k", priority_score=1.0,
                confidence=0.1,
            )
            message = compose(plan, ctx)
            assert message.template_name == f"vera_{action.value}_v1"
            assert re.findall(r"\d", message.body) == [], message.body


# --------------------------------------------------------------------------- #
# B. NO_ACTION cannot become a message
# --------------------------------------------------------------------------- #


def test_no_action_plan_raises_typed_error() -> None:
    ctx = seed_context(IPL, trigger=seed_context_parts(IPL)["trigger"] | {"payload": seed_context_parts(IPL)["trigger"]["payload"] | {"city": "Mumbai"}})
    plan = select(ctx)
    assert plan.is_no_action

    with pytest.raises(CompositionError, match="no_action"):
        compose(plan, ctx)
    assert issubclass(CompositionError, ValueError)


def test_context_of_another_trigger_is_refused() -> None:
    plan, _ = seed_plan(DIGEST)
    with pytest.raises(CompositionError, match="context"):
        compose(plan, seed_context(COMPLIANCE))


# --------------------------------------------------------------------------- #
# C. Determinism
# --------------------------------------------------------------------------- #


def test_composition_is_deterministic_and_time_independent(composed) -> None:
    for plan, ctx, message in composed[:60]:
        later = ctx.model_copy(update={"now": SEED_NOW.replace(year=2027)})
        assert compose(plan, ctx) == message == compose(plan, later)


def test_same_plan_from_fresh_contexts_gives_identical_output() -> None:
    first = [compose(*seed_plan(t)) for t in (DIGEST, RECALL, IPL, REFILL, WINBACK)]
    second = [compose(*seed_plan(t)) for t in (DIGEST, RECALL, IPL, REFILL, WINBACK)]
    assert first == second


# --------------------------------------------------------------------------- #
# E. Grounding
# --------------------------------------------------------------------------- #


def test_every_fact_reference_resolves_in_the_context(composed) -> None:
    for _, ctx, message in composed:
        for ref in message.facts_used:
            source, _, path = ref.partition(":")
            assert ctx.value(EvidenceSource(source), path) is not None, ref


def numeric_values(value: Any) -> set[float]:
    if isinstance(value, bool):
        return set()
    if isinstance(value, (int, float)):
        return {float(value)}
    if isinstance(value, dict):
        return set().union(*(numeric_values(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(numeric_values(v) for v in value))
    return set()


def test_every_number_in_a_body_appears_in_the_context(composed) -> None:
    """Digits come from context text, or are a context fraction rendered as a percentage by Phase 2B."""
    for plan, ctx, message in composed:
        text = context_text(ctx)
        fractions = {round(abs(v) * 100, 1) for v in numeric_values([ctx.category, ctx.merchant, ctx.trigger, ctx.customer])}
        for number in re.findall(r"\d+(?:\.\d+)?", message.body):
            assert number in text or float(number) in fractions, (plan.trigger_id, number, message.body)


def test_template_with_params_reads_as_the_body(composed) -> None:
    for _, _, message in composed:
        assert reads_as(message) == message.body
        assert message.template.count("{{") == len(message.template_params)


def test_research_digest_cites_its_source() -> None:
    message = compose(*seed_plan(DIGEST))
    source = seed_context_parts(DIGEST)["category"]["digest"][0]["source"]

    assert f"Source: {source}." in message.body
    assert "category:digest.0.source" in message.facts_used


def test_ungrounded_evidence_is_not_rendered() -> None:
    """A plan whose evidence no longer matches the context loses that fact instead of asserting it."""
    plan, ctx = seed_plan(RECALL)
    parts = seed_context_parts(RECALL)
    parts["trigger"]["payload"]["available_slots"] = [{"iso": "2026-11-20T10:00:00+05:30", "label": "Fri 20 Nov, 10am"}]
    message = compose(plan, CandidateGenerationContext(**parts, now=SEED_NOW))

    assert "Wed 5 Nov" not in message.body and "Fri 20 Nov" not in message.body
    assert last_sentence(message.body) == "Reply CONFIRM to book your next visit."


# --------------------------------------------------------------------------- #
# F. Missing facts are omitted, never invented
# --------------------------------------------------------------------------- #


def test_missing_owner_name_drops_the_salutation() -> None:
    parts = seed_context_parts(DIGEST)
    del parts["merchant"]["identity"]["owner_first_name"]
    ctx = CandidateGenerationContext(**parts, now=SEED_NOW)
    message = compose(select(ctx), ctx)

    assert not message.body.startswith("Dr.")
    assert message.body.startswith("New research:")


def test_customer_without_a_name_is_greeted_without_one() -> None:
    parts = seed_context_parts(RECALL)
    parts["customer"]["identity"]["name"] = "(walk-in, no profile)"
    ctx = CandidateGenerationContext(**parts, now=SEED_NOW)
    message = compose(select(ctx), ctx)

    assert message.body.startswith("Namaste, Dr. Meera's Dental Clinic here.")
    assert "walk-in" not in message.body


def test_parent_is_addressed_for_a_child_customer() -> None:
    message = compose(*seed_plan(KIDS_TRIAL))
    assert message.body.startswith("Hi Sumitra, Zen Yoga Studio here.")
    assert "parent" not in message.body


def test_taboo_words_never_reach_the_body() -> None:
    parts = seed_context_parts(DIGEST)
    parts["category"]["voice"]["vocab_taboo"] = [*parts["category"]["voice"]["vocab_taboo"], "fluoride"]
    ctx = CandidateGenerationContext(**parts, now=SEED_NOW)
    message = compose(select(ctx), ctx)

    assert "fluoride" not in message.body.lower()


def test_no_category_taboo_in_any_body(composed) -> None:
    for plan, ctx, message in composed:
        taboos = [t.lower() for t in ctx.category.get("voice", {}).get("vocab_taboo") or []]
        assert not [t for t in taboos if t in message.body.lower()], (plan.trigger_id, message.body)


def test_humanize_formats_without_adding_facts() -> None:
    assert humanize("2026-12-15") == "15 Dec 2026"
    assert humanize("2026-05-02T19:00:00+05:30") == "2 May 2026, 7pm"
    assert humanize("2026-04-26T19:30:00+05:30") == "26 Apr 2026, 7:30pm"
    assert humanize("2026-04-28T00:00:00+05:30") == "28 Apr 2026"
    assert humanize("6_month_cleaning") == "6 month cleaning"
    assert humanize("free text with_underscore stays") == "free text with_underscore stays"


# --------------------------------------------------------------------------- #
# G/H. CTA mapping and exactly one CTA
# --------------------------------------------------------------------------- #


def test_wire_cta_mapping_is_one_to_one_onto_existing_values() -> None:
    assert WIRE_CTA == {
        CTAType.NONE: CtaType.NONE,
        CTAType.YES_NO: CtaType.BINARY_YES_NO,
        CTAType.OPEN_ENDED: CtaType.OPEN_ENDED,
        CTAType.CONFIRMATION: CtaType.BINARY_CONFIRM_CANCEL,
    }


def test_every_message_carries_its_plans_cta_as_the_last_sentence(composed) -> None:
    for plan, _, message in composed:
        body, last = message.body, last_sentence(message.body)
        assert message.cta is WIRE_CTA[plan.cta_type]
        if plan.cta_type in {CTAType.YES_NO, CTAType.OPEN_ENDED}:
            assert body.count("?") == 1 and body.endswith("?"), body
            assert "Reply CONFIRM" not in body
        elif plan.cta_type is CTAType.CONFIRMATION:
            assert "?" not in body and last.startswith("Reply CONFIRM"), body
            assert body.count("Reply CONFIRM") == 1
        else:
            assert "?" not in body and "Reply CONFIRM" not in body, body


# --------------------------------------------------------------------------- #
# I/J. Voice: send_as, no internals for customers, no executed-action claims
# --------------------------------------------------------------------------- #


def test_send_as_follows_the_plan(composed) -> None:
    for plan, _, message in composed:
        assert message.send_as is plan.send_as
        expected = SendAs.MERCHANT_ON_BEHALF if plan.scope is DecisionScope.CUSTOMER else SendAs.VERA
        assert message.send_as is expected


def test_customer_messages_speak_as_the_business_and_leak_no_internals(composed) -> None:
    customer = [(p, c, m) for p, c, m in composed if p.scope is DecisionScope.CUSTOMER]
    assert customer
    for plan, ctx, message in customer:
        business = ctx.merchant["identity"]["name"]
        assert re.match(rf"^(Hi|Namaste)( [^,]+)?, {re.escape(business)} here\. ", message.body), message.body
        leaked = [w for w in (*INTERNAL_MARKERS, *CUSTOMER_FORBIDDEN) if w in message.body]
        assert not leaked, (plan.trigger_id, leaked, message.body)
        assert plan.trigger_id not in message.body and plan.suppression_key not in message.body


def test_merchant_messages_leak_no_internals(composed) -> None:
    for plan, _, message in composed:
        assert not [w for w in INTERNAL_MARKERS if w in message.body], message.body
        assert plan.trigger_id not in message.body and plan.suppression_key not in message.body


def test_no_message_claims_an_action_was_executed(composed) -> None:
    claims = re.compile(r"\b(I (have )?(sent|drafted|booked|scheduled|posted|updated|fixed)|has been (sent|booked)|done)\b", re.I)
    for _, _, message in composed:
        assert not claims.search(message.body), message.body


def test_draft_actions_offer_to_draft() -> None:
    for trigger_id in (DIGEST, IPL):
        message = compose(*seed_plan(trigger_id))
        assert "I can draft" in message.body and message.body.endswith("Want me to draft it?")


# --------------------------------------------------------------------------- #
# K/L. Template names and params
# --------------------------------------------------------------------------- #


def test_template_names_are_stable_per_action(composed) -> None:
    for plan, _, message in composed:
        assert message.template_name == f"vera_{plan.action.value}_{TEMPLATE_VERSION}" == f"vera_{plan.action.value}_v1"


def test_params_are_only_values_inserted_into_the_body(composed) -> None:
    for plan, _, message in composed:
        assert all(isinstance(p, str) and p and p in message.body for p in message.template_params)
        internals = {plan.trigger_id, plan.suppression_key, plan.plan_id, str(plan.priority_score), str(plan.confidence)}
        assert not internals & set(message.template_params)
        assert not set(message.template_params) & set(plan.rationale_facts)


def test_params_differ_from_the_rationale_dump() -> None:
    plan, ctx = seed_plan(DIGEST)
    message = compose(plan, ctx)
    assert list(message.template_params) != list(plan.rationale_facts)
    assert not any(p.startswith("trigger kind") for p in message.template_params)


# --------------------------------------------------------------------------- #
# Archetype and category voice
# --------------------------------------------------------------------------- #


def test_merchant_salutation_follows_category_voice() -> None:
    assert compose(*seed_plan(DIGEST)).body.startswith("Dr. Meera \u2014 ")
    assert compose(*seed_plan(PERF_DIP)).body.startswith("Dr. Bharat \u2014 ")
    assert compose(*seed_plan(CURIOUS)).body.startswith("Hi Lakshmi \u2014 ")
    assert compose(*seed_plan(IPL)).body.startswith("Hi Suresh \u2014 ")


def test_safety_compliance_is_factual_with_deadline_and_source() -> None:
    message = compose(*seed_plan(COMPLIANCE))
    assert "Deadline: 15 Dec 2026." in message.body
    assert "Source: Dental Council of India circular 2026-11-04." in message.body
    assert "!" not in message.body


def test_performance_change_is_stated_with_direction() -> None:
    message = compose(*seed_plan(PERF_DIP))
    assert "Your calls are down 50%." in message.body
    assert message.body.count("verifying your Google profile") == 1


def test_open_ended_ask_is_a_single_question() -> None:
    message = compose(*seed_plan(CURIOUS))
    assert message.body == "Hi Lakshmi \u2014 What service have customers asked for most this week?"
    assert message.cta is CtaType.OPEN_ENDED


@cache
def _seed_customer_actions() -> dict[str, ActionType]:
    return {t: seed_plan(t)[0].action for t in (RECALL, REFILL, WINBACK, KIDS_TRIAL)}


def test_customer_actions_use_customer_templates() -> None:
    assert _seed_customer_actions() == {
        RECALL: A.SEND_CUSTOMER_REMINDER, REFILL: A.SEND_CUSTOMER_REMINDER,
        WINBACK: A.SEND_CUSTOMER_WINBACK, KIDS_TRIAL: A.SEND_CUSTOMER_FOLLOWUP,
    }
    refill = compose(*seed_plan(REFILL))
    assert refill.body.endswith("Reply CONFIRM to arrange your refill.")
    assert "28 Apr 2026" in refill.body
