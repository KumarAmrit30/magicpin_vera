"""Phase 2D selection across the 30 canonical pairs and the full expanded dataset.

The expanded dataset is generated into a pytest temp dir (the ``expanded``
fixture); nothing is written into the repository.
"""

from collections import Counter
from datetime import datetime
from typing import Any

import pytest

from app.engine import ActionType, CTAType, DecisionPlan, DecisionScope, SendAs
from app.engine import features as features_module
from app.engine.candidates import CandidateGenerationContext, generate_candidates
from app.engine.eligibility import EligibilityResult, evaluate_candidates
from app.engine.selection import rank_eligible, select_decision
from app.state.suppression_store import SuppressionStore
from tests.conftest import SEED_NOW, expanded_context, requires_dataset

pytestmark = requires_dataset

A = ActionType

AFTER_EVERY_EXPIRY = datetime.fromisoformat("2026-12-16T00:00:00+00:00")


def _results(ctx: CandidateGenerationContext, now: datetime = SEED_NOW) -> list[EligibilityResult]:
    return list(evaluate_candidates(generate_candidates(ctx), ctx, SuppressionStore(), now=now))


def _plans(data: dict[str, Any], now: datetime = SEED_NOW) -> dict[str, DecisionPlan]:
    plans = {}
    for trigger_id in sorted(data["triggers"]):
        ctx = expanded_context(data, trigger_id)
        plans[trigger_id] = select_decision(ctx, _results(ctx, now))
    return plans


def _assert_valid(plan: DecisionPlan, ctx: CandidateGenerationContext) -> None:
    assert DecisionPlan.model_validate(plan.model_dump()) == plan
    assert (plan.trigger_id, plan.merchant_id) == (ctx.trigger_id, ctx.merchant_id)
    assert plan.send_as is (SendAs.MERCHANT_ON_BEHALF if plan.scope is DecisionScope.CUSTOMER else SendAs.VERA)
    assert plan.action is not A.NO_ACTION or plan.cta_type is CTAType.NONE
    assert all(ctx.is_grounded(e) for e in plan.evidence)
    assert (plan.suppression_key, plan.expires_at) == (ctx.suppression_key, ctx.expires_at)
    assert 0.0 <= plan.priority_score <= 100.0 and 0.0 <= plan.confidence <= 1.0


def test_every_canonical_pair_selects_one_valid_plan(expanded: dict[str, Any]) -> None:
    assert len(expanded["pairs"]) == 30

    for pair in expanded["pairs"]:
        ctx = expanded_context(expanded, pair["trigger_id"])
        results = _results(ctx)
        plan = select_decision(ctx, results)

        _assert_valid(plan, ctx)
        top = rank_eligible(results)[0]
        assert (plan.action, plan.priority_score) == (top.candidate.action, top.score), pair["test_id"]


def test_canonical_winners_are_invariant_under_candidate_order(expanded: dict[str, Any]) -> None:
    for pair in expanded["pairs"]:
        ctx = expanded_context(expanded, pair["trigger_id"])
        results = _results(ctx)
        expected = select_decision(ctx, results)

        for shift in range(len(results)):
            rotated = results[shift:] + results[:shift]
            assert select_decision(ctx, rotated) == expected, pair["test_id"]
            assert select_decision(ctx, rotated[::-1]) == expected, pair["test_id"]


def test_expanded_dataset_yields_one_valid_plan_per_trigger(expanded: dict[str, Any]) -> None:
    plans = _plans(expanded)

    assert len(plans) == 100
    for trigger_id, plan in plans.items():
        _assert_valid(plan, expanded_context(expanded, trigger_id))
    actions = Counter(plan.action for plan in plans.values())
    assert actions[A.NO_ACTION] < 50


def test_scope_alignment_leaves_merchant_triggers_alone_and_keeps_drafts_competing(expanded: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    ranked = {tid: rank_eligible(_results(expanded_context(expanded, tid))) for tid in sorted(expanded["triggers"])}
    monkeypatch.setattr(features_module, "addresses_trigger_subject", lambda ctx, scope: True)

    for trigger_id, ranking in ranked.items():
        if expanded["triggers"][trigger_id]["scope"] == "merchant":
            assert ranking == rank_eligible(_results(expanded_context(expanded, trigger_id))), trigger_id
        else:
            assert A.DRAFT_MESSAGE in {r.candidate.action for r in ranking}, trigger_id


def test_expanded_selection_is_deterministic(expanded: dict[str, Any]) -> None:
    assert _plans(expanded) == _plans(expanded)


def test_after_every_expiry_every_decision_is_no_action(expanded: dict[str, Any]) -> None:
    plans = _plans(expanded, AFTER_EVERY_EXPIRY)

    assert all(plan.action is A.NO_ACTION and plan.cta_type is CTAType.NONE for plan in plans.values())
