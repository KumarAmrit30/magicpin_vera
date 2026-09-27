"""Phase 2D: scoring, ranking and winner selection into one DecisionPlan.

Pure domain tests: seed contexts, Phase 2B candidates, Phase 2C eligibility,
no FastAPI.
"""

import ast
import itertools
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.engine import ActionType, CTAType, DecisionCandidate, DecisionPlan, DecisionScope, SendAs, make_plan_id
from app.engine import selection as selection_module
from app.engine.candidates import CandidateGenerationContext, generate_candidates
from app.engine.eligibility import EligibilityReason, EligibilityReasonCode, EligibilityResult, evaluate_candidates
from app.engine.scoring import rank_candidates, score_candidate
from app.engine.selection import (
    NO_CANDIDATES_OBJECTIVE,
    NO_ELIGIBLE_OBJECTIVE,
    UnplannableTriggerError,
    decision_confidence,
    plan_from_candidate,
    rank_eligible,
    rationale_facts,
    select_decision,
)
from app.state.suppression_store import SuppressionStore
from tests.conftest import CANDIDATE_FEATURES, SEED_NOW, candidate_fields, requires_dataset, seed_context, seed_context_parts

pytestmark = requires_dataset

A = ActionType
R = EligibilityReasonCode

DIGEST = "trg_001_research_digest_dentists"
RECALL = "trg_003_recall_due_priya"
FESTIVAL = "trg_006_festival_diwali"
IPL = "trg_010_ipl_match_delhi"
SEASONAL = "trg_014_seasonal_acquisition_dip_powerhouse"
CUSTOMER_WINBACK = "trg_015_winback_rashmi"
PERF_SPIKE = "trg_024_perf_spike_zen"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def cand(**overrides: Any) -> DecisionCandidate:
    """A DIGEST-trigger merchant candidate; every feature 0.5 unless overridden."""
    return DecisionCandidate(**candidate_fields(**overrides))


def ok(candidate: DecisionCandidate) -> EligibilityResult:
    return EligibilityResult(candidate=candidate, eligible=True)


def rejected(candidate: DecisionCandidate, code: EligibilityReasonCode = R.SUPPRESSED) -> EligibilityResult:
    return EligibilityResult(candidate=candidate, eligible=False, reasons=(EligibilityReason(code=code, detail="x"),))


def pipeline(context: CandidateGenerationContext, suppression: SuppressionStore | None = None, now: datetime = SEED_NOW) -> tuple[EligibilityResult, ...]:
    return evaluate_candidates(generate_candidates(context), context, suppression or SuppressionStore(), now=now)


def plan_for(trigger_id: str, **overrides: Any) -> DecisionPlan:
    context = seed_context(trigger_id, **overrides)
    return select_decision(context, pipeline(context))


def winner_rank(results: list[EligibilityResult]) -> DecisionCandidate:
    return rank_eligible(results)[0].candidate


# --------------------------------------------------------------------------- #
# Scoring and ranking reuse Phase 2A unchanged
# --------------------------------------------------------------------------- #


def test_ranking_scores_are_phase_2a_scores() -> None:
    results = pipeline(seed_context(FESTIVAL))

    ranking = rank_eligible(results)

    assert [r.candidate for r in ranking] == rank_candidates(r.candidate for r in results)
    assert [r.score for r in ranking] == [score_candidate(r.candidate) for r in ranking]
    assert [r.rank for r in ranking] == list(range(1, len(ranking) + 1))


def test_winner_is_the_top_ranked_eligible_candidate() -> None:
    context = seed_context(IPL)
    results = pipeline(context)

    plan = select_decision(context, results)
    top = rank_eligible(results)[0]

    assert (plan.action, plan.priority_score, plan.selected_offer_id) == (top.candidate.action, top.score, top.candidate.selected_offer_id)


def test_selection_does_not_alter_candidate_features() -> None:
    context = seed_context(IPL)
    results = pipeline(context)
    before = [r.candidate.model_dump() for r in results]

    select_decision(context, results)
    rank_eligible(results)

    assert [r.candidate.model_dump() for r in results] == before


def test_tie_break_exposes_the_sort_key_without_the_score() -> None:
    c = cand(urgency=0.7, expires_at="2026-05-03T00:00:00Z")

    entry = rank_eligible([ok(c)])[0]

    assert entry.tie_break[:5] == (-0.7, -0.5, -0.5, (False, c.expires_at), c.trigger_id)
    assert entry.decision_id == make_plan_id(
        merchant_id=c.merchant_id, customer_id=None, trigger_id=c.trigger_id,
        objective=c.objective, action=c.action, suppression_key=c.suppression_key,
    )


# --------------------------------------------------------------------------- #
# Eligibility comes first
# --------------------------------------------------------------------------- #


def test_high_scoring_ineligible_candidate_cannot_beat_eligible_one() -> None:
    strong = cand(action="send_alert", **dict.fromkeys(CANDIDATE_FEATURES, 0.95))
    weaker = cand(action="send_insight", **dict.fromkeys(CANDIDATE_FEATURES, 0.75))
    assert score_candidate(strong) == 95.0 and score_candidate(weaker) == 75.0

    plan = select_decision(seed_context(DIGEST), [rejected(strong), ok(weaker)])

    assert plan.action is A.SEND_INSIGHT
    assert plan.priority_score == 75.0


@pytest.mark.parametrize("code", list(EligibilityReasonCode))
def test_no_rejection_reason_lets_a_candidate_through(code: EligibilityReasonCode) -> None:
    strong = cand(action="send_alert", **dict.fromkeys(CANDIDATE_FEATURES, 1.0))

    ranking = rank_eligible([rejected(strong, code), ok(cand())])

    assert [r.candidate.action for r in ranking] == [A.SEND_INSIGHT]


def test_real_pipeline_offer_expiry_hands_the_win_to_the_next_candidate() -> None:
    context = seed_context(IPL)
    winner = select_decision(context, pipeline(context))
    assert winner.action is A.DRAFT_CAMPAIGN and winner.selected_offer_id == "o_skpz_001"

    parts = seed_context_parts(IPL)
    for offer in parts["merchant"]["offers"]:
        if offer["id"] == "o_skpz_001":
            offer["status"] = "expired"
    current = CandidateGenerationContext(**parts, now=SEED_NOW)
    results = evaluate_candidates(generate_candidates(context), current, SuppressionStore(), now=SEED_NOW)

    plan = select_decision(current, results)

    assert [r.reason_codes for r in results if r.candidate.action is A.DRAFT_CAMPAIGN] == [(R.OFFER_UNAVAILABLE,)]
    assert (plan.action, plan.selected_offer_id) == (A.SEND_INSIGHT, None)


# --------------------------------------------------------------------------- #
# NO_ACTION: fallbacks and competition
# --------------------------------------------------------------------------- #


def test_no_candidates_yields_a_no_action_plan() -> None:
    context = seed_context(DIGEST)

    plan = select_decision(context, [])

    assert plan.action is A.NO_ACTION and plan.cta_type is CTAType.NONE
    assert plan.objective == NO_CANDIDATES_OBJECTIVE
    assert plan.evidence == ()
    assert plan.rationale_facts == ("candidates: 0",)
    assert (plan.trigger_id, plan.merchant_id, plan.suppression_key, plan.expires_at) == (
        context.trigger_id, context.merchant_id, context.suppression_key, context.expires_at,
    )
    assert (plan.priority_score, plan.confidence) == (0.0, 1.0)


def test_all_ineligible_yields_a_no_action_plan_naming_the_reasons() -> None:
    context = seed_context(DIGEST)
    store = SuppressionStore()
    store.suppress(context.suppression_key)
    results = pipeline(context, store)
    assert results and not any(r.eligible for r in results)

    plan = select_decision(context, results)

    assert plan.action is A.NO_ACTION and plan.objective == NO_ELIGIBLE_OBJECTIVE
    assert plan.rationale_facts == (f"candidates: {len(results)}", "eligible: 0", "rejected: suppressed")
    assert plan.evidence == ()


def test_all_ineligible_reasons_are_listed_once_in_code_order() -> None:
    context = seed_context(DIGEST)
    results = [rejected(cand(), R.SUPPRESSED), rejected(cand(action="send_alert"), R.TRIGGER_EXPIRED), rejected(cand(), R.SUPPRESSED)]

    plan = select_decision(context, results)

    assert plan.rationale_facts == ("candidates: 3", "eligible: 0", "rejected: trigger_expired", "rejected: suppressed")


def test_fallback_for_customer_trigger_is_customer_scoped_like_phase_2b() -> None:
    context = seed_context(RECALL)

    plan = select_decision(context, [])

    assert (plan.scope, plan.customer_id, plan.send_as) == (DecisionScope.CUSTOMER, "c_001_priya_for_m001", SendAs.MERCHANT_ON_BEHALF)


def test_unmapped_trigger_with_nothing_eligible_cannot_be_planned() -> None:
    parts = seed_context_parts(DIGEST)
    parts["trigger"]["kind"] = "totally_unknown_kind"
    context = CandidateGenerationContext(**parts, now=SEED_NOW)
    assert generate_candidates(context) == []

    with pytest.raises(UnplannableTriggerError):
        select_decision(context, [])


def test_no_action_does_not_win_automatically_against_actionable() -> None:
    context = seed_context(SEASONAL)
    results = pipeline(context)
    assert A.NO_ACTION in {r.candidate.action for r in results}

    plan = select_decision(context, results)

    assert plan.action is not A.NO_ACTION
    assert [r.candidate.action for r in rank_eligible(results)][-1] is A.NO_ACTION


def test_no_action_can_win_on_its_features() -> None:
    restraint = cand(
        action="no_action", cta_type="none", objective="stay quiet",
        urgency=0.0, time_pressure=0.0, conversation_relevance=0.0, engagement_potential=0.0,
        merchant_relevance=1.0, actionability=1.0, evidence_strength=1.0,
    )
    weak = cand(action="send_insight", **dict.fromkeys(CANDIDATE_FEATURES, 0.2))
    assert score_candidate(restraint) == 40.0 > score_candidate(weak) == 20.0

    plan = select_decision(seed_context(DIGEST), [ok(weak), ok(restraint)])

    assert plan.action is A.NO_ACTION and plan.cta_type is CTAType.NONE
    assert plan.priority_score == 40.0


def test_only_no_action_candidates_select_the_generated_no_action() -> None:
    context = seed_context(FESTIVAL)
    only_restraint = [r for r in pipeline(context) if r.candidate.action is A.NO_ACTION]

    plan = select_decision(context, only_restraint)

    assert plan.action is A.NO_ACTION
    assert plan.objective == only_restraint[0].candidate.objective
    assert plan.evidence == only_restraint[0].candidate.evidence


# --------------------------------------------------------------------------- #
# Tie-breaking (Phase 2A candidate_sort_key)
# --------------------------------------------------------------------------- #


def _assert_first(first: DecisionCandidate, second: DecisionCandidate) -> None:
    for order in ([first, second], [second, first]):
        assert winner_rank([ok(c) for c in order]) == first


def test_tie_on_score_breaks_on_urgency() -> None:
    a = cand(action="send_alert", urgency=0.7, actionability=0.0)
    b = cand(action="send_insight")
    assert score_candidate(a) == score_candidate(b) == 50.0

    _assert_first(a, b)


def test_tie_on_score_and_urgency_breaks_on_evidence_strength() -> None:
    a = cand(action="send_insight", evidence_strength=0.7, actionability=0.3)
    b = cand(action="ask_merchant")
    assert score_candidate(a) == score_candidate(b)

    _assert_first(a, b)


def test_tie_through_evidence_breaks_on_conversation_relevance() -> None:
    a = cand(action="send_insight", conversation_relevance=0.7, actionability=0.2)
    b = cand(action="ask_merchant")
    assert score_candidate(a) == score_candidate(b)

    _assert_first(a, b)


def test_tie_through_conversation_breaks_on_earlier_expiry() -> None:
    sooner = cand(action="send_insight", expires_at="2026-05-01T00:00:00Z")
    later = cand(action="ask_merchant", expires_at="2026-05-03T00:00:00Z")
    never = cand(action="draft_post")

    _assert_first(sooner, later)
    _assert_first(later, never)


def test_tie_through_expiry_breaks_on_lexical_trigger_id() -> None:
    a = cand(trigger_id="trg_a", action="send_insight")
    b = cand(trigger_id="trg_b", action="ask_merchant")

    _assert_first(a, b)


@pytest.mark.parametrize(
    "first, second",
    [
        ({"action": "ask_merchant"}, {"action": "send_insight"}),
        ({"objective": "a objective"}, {"objective": "b objective"}),
        ({"suppression_key": "a:key"}, {"suppression_key": "b:key"}),
        ({"action": "draft_campaign", "selected_offer_id": "o_a"}, {"action": "draft_campaign", "selected_offer_id": "o_b"}),
        ({"cta_type": "none"}, {"cta_type": "yes_no"}),
    ],
    ids=["action", "objective", "suppression_key", "selected_offer_id", "cta_type"],
)
def test_secondary_identity_keys_make_the_order_total(first: dict, second: dict) -> None:
    _assert_first(cand(**first), cand(**second))


def test_identical_candidates_produce_identical_plans() -> None:
    c = cand()

    plan = select_decision(seed_context(DIGEST), [ok(c), ok(c)])

    assert plan == plan_from_candidate(c, priority_score=50.0, confidence=decision_confidence(50.0, 50.0, 0.5))


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("trigger_id", [DIGEST, FESTIVAL, PERF_SPIKE, CUSTOMER_WINBACK, "trg_023_competitor_opened_dentist"])
def test_winner_is_invariant_under_every_input_permutation(trigger_id: str) -> None:
    context = seed_context(trigger_id)
    results = list(pipeline(context))
    expected = select_decision(context, results)

    for order in itertools.permutations(results):
        assert select_decision(context, list(order)) == expected


def test_repeated_selection_is_identical() -> None:
    plans = {plan_for(FESTIVAL).model_dump_json() for _ in range(5)}

    assert len(plans) == 1


def test_dictionary_key_order_does_not_change_the_plan() -> None:
    parts = seed_context_parts(IPL)
    shuffled = {key: dict(reversed(list(value.items()))) if isinstance(value, dict) else value for key, value in reversed(list(parts.items()))}

    left = CandidateGenerationContext(**parts, now=SEED_NOW)
    right = CandidateGenerationContext(**shuffled, now=SEED_NOW)

    assert select_decision(left, pipeline(left)) == select_decision(right, pipeline(right))


def test_plan_id_is_stable_across_processes() -> None:
    plan = plan_for(IPL)
    code = (
        "from tests.conftest import seed_context, SEED_NOW;"
        "from app.engine.candidates import generate_candidates;"
        "from app.engine.eligibility import evaluate_candidates;"
        "from app.engine.selection import select_decision;"
        "from app.state.suppression_store import SuppressionStore;"
        f"c = seed_context({IPL!r});"
        "print(select_decision(c, evaluate_candidates(generate_candidates(c), c, SuppressionStore(), now=SEED_NOW)).plan_id)"
    )
    out = subprocess.run([sys.executable, "-W", "ignore", "-c", code], check=True, capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)

    assert out.stdout.strip() == plan.plan_id


# --------------------------------------------------------------------------- #
# Mixed scopes, actions and offers: features decide, nothing is hardcoded
# --------------------------------------------------------------------------- #


def test_customer_and_merchant_candidates_compete_on_score_only() -> None:
    customer = dict(scope="customer", customer_id="c_1", action="send_customer_reminder", send_as="merchant_on_behalf", cta_type="confirmation")
    merchant = dict(action="draft_message", cta_type="yes_no")

    assert winner_rank([ok(cand(**customer, urgency=0.6)), ok(cand(**merchant))]).scope is DecisionScope.CUSTOMER
    assert winner_rank([ok(cand(**customer)), ok(cand(**merchant, urgency=0.6))]).scope is DecisionScope.MERCHANT


@pytest.mark.parametrize("stronger", ["draft_campaign", "send_insight"])
def test_action_type_has_no_built_in_preference(stronger: str) -> None:
    other = "send_insight" if stronger == "draft_campaign" else "draft_campaign"

    winner = winner_rank([ok(cand(action=stronger, merchant_relevance=0.6)), ok(cand(action=other))])

    assert winner.action.value == stronger


def test_real_customer_trigger_ranking_follows_scores() -> None:
    context = seed_context(CUSTOMER_WINBACK)
    ranking = rank_eligible(pipeline(context))

    assert {r.candidate.scope for r in ranking} == {DecisionScope.CUSTOMER, DecisionScope.MERCHANT}
    assert [r.score for r in ranking] == sorted((r.score for r in ranking), reverse=True)


def test_per_offer_candidates_are_ranked_independently() -> None:
    context = seed_context(PERF_SPIKE)
    ranking = rank_eligible(pipeline(context))
    offers = [r for r in ranking if r.candidate.action is A.DRAFT_CAMPAIGN]

    assert len(offers) == 2
    assert {r.candidate.selected_offer_id for r in offers} == {"o_zen_001", "o_zen_002"}
    assert len({r.rank for r in offers}) == 2


def test_winning_offer_comes_from_the_winning_candidate() -> None:
    boosted = [
        ok(cand(action="draft_campaign", selected_offer_id="o_a")),
        ok(cand(action="draft_campaign", selected_offer_id="o_b", merchant_relevance=0.6)),
        ok(cand(action="draft_campaign", selected_offer_id="o_c")),
    ]

    plan = select_decision(seed_context(DIGEST), boosted)

    assert plan.selected_offer_id == "o_b"


def test_per_offer_candidates_share_a_plan_id_under_the_phase_2a_identity() -> None:
    # make_plan_id excludes selected_offer_id (Phase 2A contract); documented, not changed.
    offers = [r for r in rank_eligible(pipeline(seed_context(PERF_SPIKE))) if r.candidate.action is A.DRAFT_CAMPAIGN]

    assert offers[0].decision_id == offers[1].decision_id


# --------------------------------------------------------------------------- #
# Plan construction and validation
# --------------------------------------------------------------------------- #


def test_plan_carries_the_winning_candidate_verbatim() -> None:
    context = seed_context(RECALL)
    results = pipeline(context)
    top = rank_eligible(results)[0].candidate

    plan = select_decision(context, results)

    shared = ("trigger_id", "archetype", "scope", "merchant_id", "customer_id", "objective", "action",
              "cta_type", "send_as", "evidence", "selected_offer_id", "suppression_key", "expires_at")
    assert {f: getattr(plan, f) for f in shared} == {f: getattr(top, f) for f in shared}
    assert plan.language_style is None and plan.tone_profile is None


def test_plan_id_is_the_phase_2a_identity_hash() -> None:
    plan = plan_for(IPL)

    assert plan.plan_id == make_plan_id(
        merchant_id=plan.merchant_id, customer_id=plan.customer_id, trigger_id=plan.trigger_id,
        objective=plan.objective, action=plan.action, suppression_key=plan.suppression_key,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"merchant_id": "m_other"},
        {"customer_id": "c_1"},
        {"trigger_id": "trg_other"},
        {"objective": "another objective"},
        {"action": "ask_merchant"},
        {"suppression_key": "other:key"},
    ],
)
def test_each_identity_component_changes_the_plan_id(change: dict) -> None:
    base = plan_from_candidate(cand(), priority_score=50.0, confidence=0.5)

    changed = plan_from_candidate(cand(**change), priority_score=50.0, confidence=0.5)

    assert changed.plan_id != base.plan_id


def test_score_and_confidence_do_not_change_the_plan_id() -> None:
    c = cand()

    assert plan_from_candidate(c, priority_score=10.0, confidence=0.1).plan_id == plan_from_candidate(c, priority_score=90.0, confidence=0.9).plan_id


def test_plan_id_does_not_depend_on_candidate_order() -> None:
    context = seed_context(FESTIVAL)
    results = list(pipeline(context))

    assert select_decision(context, results).plan_id == select_decision(context, results[::-1]).plan_id


def test_every_seed_plan_satisfies_domain_invariants() -> None:
    from tests.conftest import _seed_index

    for trigger_id in sorted(_seed_index()["trigger"]):
        context = seed_context(trigger_id)
        plan = select_decision(context, pipeline(context))

        assert DecisionPlan.model_validate(plan.model_dump()) == plan
        assert plan.scope is not DecisionScope.CUSTOMER or plan.customer_id is not None
        assert not plan.action.targets_customer or plan.scope is DecisionScope.CUSTOMER
        assert plan.send_as is (SendAs.MERCHANT_ON_BEHALF if plan.scope is DecisionScope.CUSTOMER else SendAs.VERA)
        assert plan.action is not A.NO_ACTION or plan.cta_type is CTAType.NONE
        assert 0.0 <= plan.priority_score <= 100.0 and 0.0 <= plan.confidence <= 1.0
        assert all(context.is_grounded(e) for e in plan.evidence)
        assert (plan.suppression_key, plan.expires_at) == (context.suppression_key, context.expires_at)


def test_eligible_candidate_from_another_trigger_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="does not belong"):
        select_decision(seed_context(DIGEST), [ok(cand(trigger_id="trg_other"))])


def test_ineligible_candidate_from_another_trigger_is_ignored() -> None:
    plan = select_decision(seed_context(DIGEST), [rejected(cand(trigger_id="trg_other"), R.TRIGGER_MISMATCH)])

    assert plan.action is A.NO_ACTION


def test_non_result_input_is_rejected() -> None:
    with pytest.raises(TypeError):
        select_decision(seed_context(DIGEST), [cand()])  # type: ignore[list-item]


# --------------------------------------------------------------------------- #
# Rationale facts
# --------------------------------------------------------------------------- #


def test_rationale_facts_are_the_evidence_renderings_by_importance() -> None:
    plan = plan_for(IPL)
    by_importance = sorted(plan.evidence, key=lambda e: -e.importance)

    assert plan.rationale_facts == tuple(dict.fromkeys(e.formatted for e in by_importance))
    assert set(plan.rationale_facts) <= {e.formatted for e in plan.evidence}


def test_rationale_facts_are_deduplicated_and_stable_for_equal_importance() -> None:
    from app.engine import Evidence, EvidenceSource

    items = (
        Evidence(source=EvidenceSource.MERCHANT, field="a", value=1, formatted="a: 1", importance=0.4),
        Evidence(source=EvidenceSource.MERCHANT, field="b", value=2, formatted="b: 2", importance=0.9),
        Evidence(source=EvidenceSource.MERCHANT, field="c", value=3, formatted="c: 3", importance=0.4),
        Evidence(source=EvidenceSource.TRIGGER, field="a", value=1, formatted="a: 1", importance=0.4),
    )

    assert rationale_facts(cand(evidence=items)) == ("b: 2", "a: 1", "c: 3")


# --------------------------------------------------------------------------- #
# Confidence: computed after selection, never used for it
# --------------------------------------------------------------------------- #


def test_confidence_single_candidate_has_full_separation() -> None:
    assert decision_confidence(60.0, None, 0.8) == 0.76  # 0.30 + 0.30 + 0.16


def test_confidence_rewards_a_large_margin() -> None:
    close = decision_confidence(82.0, 81.0, 0.8)
    clear = decision_confidence(82.0, 51.0, 0.8)

    assert (close, clear) == (0.585, 0.87)
    assert clear > close


def test_confidence_margin_saturates_at_twenty_points() -> None:
    assert decision_confidence(80.0, 60.0, 0.5) == decision_confidence(80.0, 10.0, 0.5) == decision_confidence(80.0, None, 0.5)


def test_confidence_tracks_evidence_strength() -> None:
    assert decision_confidence(60.0, 55.0, 0.2) < decision_confidence(60.0, 55.0, 0.95)


@pytest.mark.parametrize("score, runner_up, evidence", [(0.0, None, 0.0), (100.0, None, 1.0), (0.0, 0.0, 0.0), (100.0, 0.0, 1.0), (37.3, 37.3, 0.33)])
def test_confidence_is_bounded(score: float, runner_up: float | None, evidence: float) -> None:
    assert 0.0 <= decision_confidence(score, runner_up, evidence) <= 1.0


def test_confidence_rejects_a_runner_up_above_the_winner() -> None:
    with pytest.raises(ValueError):
        decision_confidence(50.0, 60.0, 0.5)


def test_no_action_winner_gets_the_same_formula() -> None:
    restraint = cand(action="no_action", cta_type="none", objective="stay quiet", merchant_relevance=1.0, evidence_strength=0.9)
    plan = select_decision(seed_context(DIGEST), [ok(restraint)])

    assert plan.confidence == decision_confidence(score_candidate(restraint), None, 0.9)


def test_plan_confidence_uses_the_runner_up_margin() -> None:
    context = seed_context(IPL)
    results = pipeline(context)
    first, second = rank_eligible(results)[:2]

    plan = select_decision(context, results)

    assert plan.confidence == decision_confidence(first.score, second.score, first.candidate.evidence_strength)


def test_confidence_never_changes_the_winner() -> None:
    # The low-evidence candidate wins on score; its lower evidence cannot flip the choice.
    winner = cand(action="send_alert", merchant_relevance=1.0, evidence_strength=0.1)
    runner = cand(action="send_insight", evidence_strength=0.9)
    assert score_candidate(winner) > score_candidate(runner)

    plan = select_decision(seed_context(DIGEST), [ok(runner), ok(winner)])

    assert plan.action is A.SEND_ALERT


# --------------------------------------------------------------------------- #
# Boundaries
# --------------------------------------------------------------------------- #


def test_selection_has_no_side_effects_on_suppression() -> None:
    context = seed_context(DIGEST)
    store = SuppressionStore()
    results = pipeline(context, store)

    select_decision(context, results)

    assert len(store) == 0


def test_selection_module_stays_inside_the_decision_boundary() -> None:
    source = Path(selection_module.__file__).read_text()
    imported = {
        node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom) and node.module
    }

    assert all(module.startswith("app.engine") for module in imported - {"collections.abc", "dataclasses", "decimal"})
    assert "suppress(" not in source and "datetime.now" not in source
    assert not any(word in source for word in ("fastapi", "httpx", "openai", "body=", "template"))


def test_selection_imports_without_fastapi() -> None:
    code = "import sys, app.engine.selection; assert 'fastapi' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).resolve().parent.parent)


def test_expiry_is_preserved_not_recomputed() -> None:
    context = seed_context(IPL)
    late = context.expires_at + timedelta(days=30)
    results = pipeline(context)  # evaluated fresh

    plan = select_decision(seed_context(IPL, now=late), results)

    assert plan.action is not A.NO_ACTION
    assert plan.expires_at == context.expires_at
