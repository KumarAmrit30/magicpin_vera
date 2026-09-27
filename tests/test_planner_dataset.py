"""Phase 2E planner across the 30 canonical pairs and the full expanded dataset.

The expanded dataset is generated into a pytest temp dir (the ``expanded``
fixture); nothing is written into the repository.
"""

from typing import Any

from app.engine.actions import ActionType, DecisionScope, SendAs
from app.engine.candidates import generate_candidates
from app.engine.eligibility import evaluate_candidates
from app.engine.planner import MAX_ACTIONS_PER_TICK, TriggerOutcome, plan_tick
from app.engine.selection import select_decision
from app.models.enums import ContextScope
from app.models.schemas import VersionedContext
from app.state.container import StateContainer
from app.state.suppression_store import SuppressionStore
from tests.conftest import SEED_NOW, FakeClock, expanded_context, requires_dataset

pytestmark = requires_dataset

O = TriggerOutcome
SCOPES = {"categories": ContextScope.CATEGORY, "merchants": ContextScope.MERCHANT, "customers": ContextScope.CUSTOMER, "triggers": ContextScope.TRIGGER}


def loaded(data: dict[str, Any]) -> StateContainer:
    clock = FakeClock()
    state = StateContainer.create(clock=clock, monotonic=clock.monotonic)
    for key, scope in SCOPES.items():
        for context_id, payload in data[key].items():
            state.context_store.put(VersionedContext(scope=scope, context_id=context_id, version=1, payload=payload, delivered_at=SEED_NOW))
    return state


def phase_2d(data: dict[str, Any], trigger_id: str):
    ctx = expanded_context(data, trigger_id)
    return select_decision(ctx, evaluate_candidates(generate_candidates(ctx), ctx, SuppressionStore(), now=SEED_NOW))


def test_every_canonical_pair_keeps_its_phase_2d_decision(expanded: dict[str, Any]) -> None:
    for pair in expanded["pairs"]:
        trigger_id = pair["trigger_id"]
        state = loaded(expanded)

        result = plan_tick(state, now=SEED_NOW, available_triggers=[trigger_id])
        (decision,) = result.decisions
        expected = phase_2d(expanded, trigger_id)

        assert decision.plan == expected, pair["test_id"]
        if expected.is_no_action:
            assert (decision.outcome, result.actions, len(state.suppression_store)) == (O.NO_ACTION, (), 0), pair["test_id"]
        else:
            (action,) = result.actions
            assert decision.outcome is O.EMITTED, pair["test_id"]
            assert (action.trigger_id, action.merchant_id, action.customer_id, action.send_as, action.suppression_key) == (
                expected.trigger_id, expected.merchant_id, expected.customer_id, expected.send_as, expected.suppression_key,
            ), pair["test_id"]


def test_customer_facing_canonical_pairs_are_sent_on_behalf_of_the_merchant(expanded: dict[str, Any]) -> None:
    expected = {"T07": ActionType.SEND_CUSTOMER_REMINDER, "T13": ActionType.SEND_CUSTOMER_WINBACK, "T28": ActionType.SEND_CUSTOMER_REMINDER}
    pairs = {p["test_id"]: p["trigger_id"] for p in expanded["pairs"] if p["test_id"] in expected}

    for test_id, trigger_id in sorted(pairs.items()):
        result = plan_tick(loaded(expanded), now=SEED_NOW, available_triggers=[trigger_id])
        (decision,) = result.decisions
        (action,) = result.actions

        assert (decision.plan.action, decision.plan.scope) == (expected[test_id], DecisionScope.CUSTOMER), test_id
        assert (action.send_as, action.customer_id) == (SendAs.MERCHANT_ON_BEHALF, decision.plan.customer_id), test_id


def test_whole_dataset_in_one_tick_is_capped_and_commits_only_emitted(expanded: dict[str, Any]) -> None:
    state = loaded(expanded)

    result = plan_tick(state, now=SEED_NOW, available_triggers=sorted(expanded["triggers"]))

    assert len(result.decisions) == 100
    assert len(result.actions) == MAX_ACTIONS_PER_TICK == len(state.conversation_store) == len(state.suppression_store)
    for decision in result.decisions:
        assert decision.plan == phase_2d(expanded, decision.trigger_id), decision.trigger_id
        committed = state.suppression_store.peek(decision.plan.suppression_key, SEED_NOW) is not None
        assert committed is (decision.outcome is O.EMITTED), decision.trigger_id


def test_judge_style_batches_emit_every_decision_once(expanded: dict[str, Any]) -> None:
    """Five triggers per tick, as ``judge_simulator.py`` does; deferred plans go out on later ticks."""
    state = loaded(expanded)
    trigger_ids = sorted(expanded["triggers"])
    emitted: list[str] = []

    for start in range(0, len(trigger_ids), 5):
        result = plan_tick(state, now=SEED_NOW, available_triggers=trigger_ids[start:start + 5])
        assert len(result.actions) <= 5
        emitted += [a.trigger_id for a in result.actions]

    actionable = [t for t in trigger_ids if not phase_2d(expanded, t).is_no_action]
    assert len(emitted) == len(set(emitted))
    assert set(emitted) <= set(actionable)
    assert len(state.suppression_store) == len(emitted) == len(state.conversation_store)
