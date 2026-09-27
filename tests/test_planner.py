"""Phase 2E tick planner: orchestration of Phases 2B-2D into emitted actions.

Runs the planner directly against a StateContainer (no FastAPI).
"""

import copy
import json
import random
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.engine.actions import ActionType, DecisionScope, SendAs
from app.engine.candidates import generate_candidates
from app.engine.eligibility import EligibilityReasonCode, evaluate_candidates
from app.engine.planner import (
    CONVERSATION_ID_PREFIX,
    MAX_ACTIONS_PER_TICK,
    PLACEHOLDER_BODY_PREFIX,
    WIRE_CTA,
    TickResult,
    TriggerOutcome,
    new_conversation_id,
    plan_tick,
    tick_action,
)
from app.engine.scoring import candidate_sort_key
from app.engine.selection import rank_eligible, select_decision
from app.models.enums import ContextScope, ConversationState, TurnRole
from app.models.schemas import TickAction, TickResponse, VersionedContext
from app.state.container import StateContainer
from app.state.suppression_store import SuppressionStore
from tests.conftest import DATASET_DIR, SEED_NOW, FakeClock, load_seed_dataset, requires_dataset, seed_context

pytestmark = requires_dataset

O = TriggerOutcome
DIGEST = "trg_001_research_digest_dentists"
COMPLIANCE = "trg_002_compliance_dci_radiograph"
RECALL = "trg_003_recall_due_priya"
IPL = "trg_010_ipl_match_delhi"
WINBACK = "trg_015_winback_rashmi"
REFILL = "trg_019_chronic_refill_grandfather"
WEBINAR = "trg_022_cde_webinar_dentists"
COMPETITOR = "trg_023_competitor_opened_dentist"
M001 = "m_001_drmeera_dentist_delhi"
M001_MERCHANT_TRIGGERS = [DIGEST, COMPLIANCE, WEBINAR, COMPETITOR]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def push(state: StateContainer, scope: str, context_id: str, payload: dict[str, Any], version: int = 1) -> None:
    state.context_store.put(
        VersionedContext(scope=ContextScope(scope), context_id=context_id, version=version, payload=payload, delivered_at=SEED_NOW)
    )


def seeded() -> StateContainer:
    clock = FakeClock()
    state = StateContainer.create(clock=clock, monotonic=clock.monotonic)
    for scope, items in load_seed_dataset().items():
        for context_id, payload in items:
            push(state, scope, context_id, payload)
    return state


def seed_payload(scope: str, context_id: str) -> dict[str, Any]:
    return copy.deepcopy(dict(load_seed_dataset()[scope])[context_id])


def tick(state: StateContainer, *triggers: str, now: datetime = SEED_NOW) -> TickResult:
    return plan_tick(state, now=now, available_triggers=list(triggers))


def outcomes(result: TickResult) -> dict[str, TriggerOutcome]:
    return {d.trigger_id: d.outcome for d in result.decisions}


def decision(result: TickResult, trigger_id: str):
    return next(d for d in result.decisions if d.trigger_id == trigger_id)


def snapshot(state: StateContainer) -> tuple:
    return (len(state.conversation_store), len(state.suppression_store))


def clone_digest(state: StateContainer, n: int, *, shared_key: bool = False) -> list[str]:
    """``n`` copies of Dr. Meera with their own research-digest trigger (distinct merchants, identical features)."""
    ids = []
    for i in range(n):
        merchant = seed_payload("merchant", M001) | {"merchant_id": f"m_9{i:02d}_clone"}
        trigger = seed_payload("trigger", DIGEST) | {
            "id": f"trg_9{i:02d}_digest_clone",
            "merchant_id": merchant["merchant_id"],
            "suppression_key": "research:clones" if shared_key else f"research:clone:{i}",
        }
        push(state, "merchant", merchant["merchant_id"], merchant)
        push(state, "trigger", trigger["id"], trigger)
        ids.append(trigger["id"])
    return ids


def phase_2d_plan(trigger_id: str, now: datetime = SEED_NOW):
    context = seed_context(trigger_id, now=now)
    return select_decision(context, evaluate_candidates(generate_candidates(context), context, SuppressionStore(), now=now))


def phase_2d_sort_key(trigger_id: str) -> tuple:
    context = seed_context(trigger_id)
    return candidate_sort_key(rank_eligible(evaluate_candidates(generate_candidates(context), context, SuppressionStore(), now=SEED_NOW))[0].candidate)


# --------------------------------------------------------------------------- #
# Basic
# --------------------------------------------------------------------------- #


def test_no_available_triggers_emits_nothing() -> None:
    state = seeded()

    result = tick(state)

    assert result == TickResult(actions=(), decisions=())
    assert snapshot(state) == (0, 0)


def test_no_action_decision_emits_nothing_and_writes_nothing() -> None:
    state = seeded()
    push(state, "trigger", IPL, seed_payload("trigger", IPL) | {"payload": seed_payload("trigger", IPL)["payload"] | {"city": "Mumbai"}}, 2)

    result = tick(state, IPL)

    assert result.actions == ()
    assert decision(result, IPL).outcome is O.NO_ACTION
    assert decision(result, IPL).plan.action is ActionType.NO_ACTION
    assert decision(result, IPL).conversation_id is None
    assert snapshot(state) == (0, 0)


def test_single_actionable_trigger_emits_exactly_one_action_from_its_plan() -> None:
    state = seeded()

    result = tick(state, DIGEST)
    plan = decision(result, DIGEST).plan

    assert len(result.actions) == 1
    action = result.actions[0]
    assert (action.trigger_id, action.merchant_id, action.customer_id, action.send_as, action.suppression_key) == (
        DIGEST, plan.merchant_id, None, SendAs.VERA, plan.suppression_key,
    )
    assert action.cta is WIRE_CTA[plan.cta_type]
    assert action.template_name == f"vera_{plan.action.value}_v0"
    assert action.template_params == list(plan.rationale_facts)
    assert action.body.startswith(f"{PLACEHOLDER_BODY_PREFIX} {plan.objective}")
    assert plan.plan_id in action.rationale
    assert decision(result, DIGEST).outcome is O.EMITTED


def test_multiple_triggers_emit_best_ranked_first() -> None:
    triggers = [DIGEST, RECALL, IPL, WINBACK, REFILL]

    result = tick(seeded(), *triggers)

    assert [a.trigger_id for a in result.actions] == sorted(triggers, key=phase_2d_sort_key)
    assert len(result.actions) == len(triggers)


def test_unknown_trigger_is_skipped() -> None:
    state = seeded()

    result = tick(state, "trg_unknown", "")

    assert result.actions == ()
    assert set(outcomes(result).values()) == {O.UNKNOWN_TRIGGER}
    assert snapshot(state) == (0, 0)


def test_unmapped_trigger_kind_is_unplannable_under_the_phase_2d_contract() -> None:
    state = seeded()
    push(state, "trigger", "trg_mystery", seed_payload("trigger", DIGEST) | {"id": "trg_mystery", "kind": "mystery_kind", "suppression_key": "mystery"})

    result = tick(state, "trg_mystery", DIGEST)

    assert decision(result, "trg_mystery").outcome is O.UNPLANNABLE
    assert (decision(result, "trg_mystery").candidates, decision(result, "trg_mystery").plan) == (0, None)
    assert [a.trigger_id for a in result.actions] == [DIGEST]


@pytest.mark.parametrize(
    ("change", "outcome", "detail"),
    [
        ({"merchant_id": "m_missing"}, O.MISSING_CONTEXT, "merchant=m_missing"),
        ({"merchant_id": None}, O.MISSING_CONTEXT, "merchant=None"),
        ({"customer_id": "c_missing"}, O.MISSING_CONTEXT, "customer=c_missing"),
        ({"customer_id": "c_004_sneha_for_m003"}, O.INVALID_CONTEXT, None),
    ],
)
def test_incomplete_or_inconsistent_context_is_skipped_without_fabrication(change: dict, outcome: TriggerOutcome, detail: str | None) -> None:
    state = seeded()
    push(state, "trigger", RECALL, seed_payload("trigger", RECALL) | change, 2)

    result = tick(state, RECALL)

    assert decision(result, RECALL).outcome is outcome
    assert detail is None or decision(result, RECALL).detail == detail
    assert result.actions == () and snapshot(state) == (0, 0)


def test_missing_category_is_skipped() -> None:
    state = seeded()
    push(state, "merchant", M001, seed_payload("merchant", M001) | {"category_slug": "florists"}, 2)

    assert decision(tick(state, DIGEST), DIGEST).outcome is O.MISSING_CONTEXT


def test_expired_trigger_emits_nothing() -> None:
    state = seeded()
    later = datetime.fromisoformat("2026-05-04T00:00:00+00:00")

    result = tick(state, DIGEST, now=later)

    assert decision(result, DIGEST).outcome is O.NO_ACTION
    assert EligibilityReasonCode.TRIGGER_EXPIRED in decision(result, DIGEST).reason_codes
    assert result.actions == () and snapshot(state) == (0, 0)


def test_planner_preserves_every_phase_2d_seed_decision() -> None:
    for trigger_id, _ in load_seed_dataset()["trigger"]:
        assert decision(tick(seeded(), trigger_id), trigger_id).plan == phase_2d_plan(trigger_id), trigger_id


@pytest.mark.parametrize(
    ("trigger_id", "action"),
    [(REFILL, ActionType.SEND_CUSTOMER_REMINDER), (WINBACK, ActionType.SEND_CUSTOMER_WINBACK), (RECALL, ActionType.SEND_CUSTOMER_REMINDER)],
)
def test_customer_facing_case_studies_are_emitted_on_behalf_of_the_merchant(trigger_id: str, action: ActionType) -> None:
    result = tick(seeded(), trigger_id)
    plan = decision(result, trigger_id).plan

    assert plan.action is action and plan.scope is DecisionScope.CUSTOMER
    assert (result.actions[0].send_as, result.actions[0].customer_id) == (SendAs.MERCHANT_ON_BEHALF, plan.customer_id)


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #


def test_merchant_action_opens_a_new_merchant_conversation() -> None:
    state = seeded()

    action = tick(state, DIGEST).actions[0]
    conversation = state.conversation_store.get(action.conversation_id)

    assert action.conversation_id.startswith(CONVERSATION_ID_PREFIX)
    assert action.conversation_id not in {M001, DIGEST} and "plan_" not in action.conversation_id
    assert (conversation.merchant_id, conversation.customer_id, conversation.trigger_id, conversation.state) == (
        M001, None, DIGEST, ConversationState.NEW,
    )
    assert [(t.role, t.body, t.sent_at) for t in conversation.turns] == [(TurnRole.VERA, action.body, SEED_NOW)]


def test_customer_action_opens_a_new_customer_conversation() -> None:
    state = seeded()

    action = tick(state, RECALL).actions[0]
    conversation = state.conversation_store.get(action.conversation_id)

    assert (conversation.merchant_id, conversation.customer_id, conversation.trigger_id) == (M001, "c_001_priya_for_m001", RECALL)


def test_existing_conversations_are_never_reused_by_a_tick() -> None:
    state = seeded()
    state.conversation_store.create("conv_merchant", merchant_id=M001)
    state.conversation_store.create("conv_priya", merchant_id=M001, customer_id="c_001_priya_for_m001")
    state.conversation_store.append_message("conv_priya", role=TurnRole.CUSTOMER, body="hi", sent_at=SEED_NOW)

    result = tick(state, DIGEST, RECALL)

    assert {a.conversation_id for a in result.actions}.isdisjoint({"conv_merchant", "conv_priya"})
    assert len(state.conversation_store) == 4
    assert state.conversation_store.get("conv_merchant").turns == []
    assert [t.body for t in state.conversation_store.get("conv_priya").turns] == ["hi"]


def test_actions_open_conversations_owned_by_their_own_recipient() -> None:
    state = seeded()
    state.conversation_store.create("conv_rohit", merchant_id=M001, customer_id="c_002_rohit_for_m001")

    result = tick(state, DIGEST, RECALL)

    for action in result.actions:
        conversation = state.conversation_store.get(action.conversation_id)
        assert (conversation.merchant_id, conversation.customer_id) == (action.merchant_id, action.customer_id)
    assert state.conversation_store.get("conv_rohit").turns == []


def test_conversation_id_collision_gets_a_fresh_suffix() -> None:
    state = seeded()
    plan = phase_2d_plan(DIGEST)
    taken = new_conversation_id(plan, SEED_NOW, lambda cid: False)
    state.conversation_store.create(taken, merchant_id="m_other")

    action = tick(state, DIGEST).actions[0]

    assert action.conversation_id == f"{taken}_2"
    assert state.conversation_store.get(taken).merchant_id == "m_other"


def test_re_emission_after_suppression_is_lifted_opens_another_conversation() -> None:
    state = seeded()
    first = tick(state, DIGEST).actions[0]
    state.suppression_store.clear(first.suppression_key)

    second = tick(state, DIGEST).actions[0]

    assert second.conversation_id != first.conversation_id
    assert len(state.conversation_store) == 2


def test_conversation_ids_are_unique_within_a_tick() -> None:
    result = tick(seeded(), *[t for t, _ in load_seed_dataset()["trigger"]])

    ids = [a.conversation_id for a in result.actions]
    assert len(ids) == len(set(ids))


def test_conversation_id_depends_on_plan_and_tick_time_only() -> None:
    plan = phase_2d_plan(DIGEST)
    same = new_conversation_id(plan, SEED_NOW, lambda cid: False)

    assert new_conversation_id(plan, SEED_NOW.astimezone(datetime.fromisoformat("2026-01-01T00:00:00+00:00").tzinfo), lambda cid: False) == same
    assert new_conversation_id(plan, SEED_NOW + timedelta(minutes=5), lambda cid: False) != same
    assert new_conversation_id(phase_2d_plan(RECALL), SEED_NOW, lambda cid: False) != same


# --------------------------------------------------------------------------- #
# One action per (merchant_id, conversation_id) pair; one per suppression key
# --------------------------------------------------------------------------- #


def test_one_merchant_may_receive_several_actions_in_one_tick_on_distinct_conversations() -> None:
    """challenge-testing-brief.md FAQ: multiple messages to one merchant per tick, one per (merchant_id, conversation_id)."""
    result = tick(seeded(), *M001_MERCHANT_TRIGGERS, RECALL)

    assert sorted(a.trigger_id for a in result.actions) == sorted([*M001_MERCHANT_TRIGGERS, RECALL])
    assert {a.merchant_id for a in result.actions} == {M001}
    assert len({(a.merchant_id, a.conversation_id) for a in result.actions}) == len(result.actions)
    assert [a.trigger_id for a in result.actions] == sorted([*M001_MERCHANT_TRIGGERS, RECALL], key=phase_2d_sort_key)


def test_every_merchant_conversation_pair_carries_at_most_one_action() -> None:
    state = seeded()
    ids = clone_digest(state, 25)

    for result in (tick(state, *ALL_SEED), tick(state, *ids)):
        pairs = [(a.merchant_id, a.conversation_id) for a in result.actions]
        assert len(pairs) == len(set(pairs))


def test_one_action_per_suppression_key_per_tick() -> None:
    state = seeded()
    ids = clone_digest(state, 3, shared_key=True)

    result = tick(state, *ids)

    assert [a.trigger_id for a in result.actions] == [ids[0]]
    assert [outcomes(result)[i] for i in ids[1:]] == [O.DUPLICATE_SUPPRESSION_KEY] * 2


# --------------------------------------------------------------------------- #
# Action cap
# --------------------------------------------------------------------------- #


def test_tick_emits_at_most_twenty_actions() -> None:
    state = seeded()
    ids = clone_digest(state, 25)

    result = tick(state, *ids)

    assert len(result.actions) == MAX_ACTIONS_PER_TICK
    assert sum(o is O.OVER_CAP for o in outcomes(result).values()) == 5
    TickResponse(actions=list(result.actions))


def test_the_surviving_twenty_are_the_best_ranked() -> None:
    state = seeded()
    ids = clone_digest(state, 25)

    result = tick(state, *ids)

    assert [a.trigger_id for a in result.actions] == sorted(ids)[:MAX_ACTIONS_PER_TICK]


def test_input_order_does_not_change_the_surviving_twenty() -> None:
    ids = clone_digest(seeded(), 25)
    expected = None
    for seed in range(4):
        state = seeded()
        clone_digest(state, 25)
        order = ids[:]
        random.Random(seed).shuffle(order)
        actions = tick(state, *order, *order[:3]).actions
        expected = expected or actions
        assert actions == expected


# --------------------------------------------------------------------------- #
# Suppression commit
# --------------------------------------------------------------------------- #


def test_emitted_action_commits_its_suppression_key() -> None:
    state = seeded()

    action = tick(state, DIGEST).actions[0]
    record = state.suppression_store.peek(action.suppression_key, SEED_NOW)

    assert record is not None and record.expires_at is None
    assert action.conversation_id in record.reason
    assert len(state.suppression_store) == 1


def test_ineligible_candidates_commit_nothing() -> None:
    state = seeded()

    result = tick(state, DIGEST, now=datetime.fromisoformat("2026-05-04T00:00:00+00:00"))

    assert decision(result, DIGEST).eligible == 0 < decision(result, DIGEST).candidates
    assert len(state.suppression_store) == 0


def test_plans_dropped_by_the_cap_commit_nothing() -> None:
    state = seeded()
    ids = clone_digest(state, 25)

    result = tick(state, *ids)

    dropped = [d for d in result.decisions if d.outcome is O.OVER_CAP]
    assert len(state.suppression_store) == MAX_ACTIONS_PER_TICK == len(state.conversation_store)
    assert all(state.suppression_store.peek(d.plan.suppression_key, SEED_NOW) is None for d in dropped)


def test_plans_dropped_as_duplicates_commit_nothing() -> None:
    state = seeded()
    ids = clone_digest(state, 3, shared_key=True)

    result = tick(state, *ids)

    dropped = [d for d in result.decisions if d.outcome is O.DUPLICATE_SUPPRESSION_KEY]
    assert len(dropped) == 2 and len(state.suppression_store) == 1 == len(state.conversation_store)
    assert all(d.conversation_id is None for d in dropped)


def test_next_tick_respects_committed_suppression() -> None:
    state = seeded()
    first = tick(state, *M001_MERCHANT_TRIGGERS)

    second = tick(state, *M001_MERCHANT_TRIGGERS, now=SEED_NOW + timedelta(minutes=5))

    assert len(first.actions) == len(M001_MERCHANT_TRIGGERS)
    assert second.actions == ()
    for trigger_id in M001_MERCHANT_TRIGGERS:
        assert decision(second, trigger_id).outcome is O.NO_ACTION
        assert EligibilityReasonCode.SUPPRESSED in decision(second, trigger_id).reason_codes


def test_suppression_is_not_committed_when_persisting_conversations_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    state = seeded()
    create = state.conversation_store.create
    calls = []

    def flaky(*args: Any, **kwargs: Any):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("store failure")
        return create(*args, **kwargs)

    monkeypatch.setattr(state.conversation_store, "create", flaky)

    with pytest.raises(RuntimeError, match="store failure"):
        tick(state, DIGEST, RECALL)
    assert len(state.suppression_store) == 0


def test_no_action_plan_cannot_become_an_action() -> None:
    state = seeded()
    push(state, "trigger", IPL, seed_payload("trigger", IPL) | {"payload": seed_payload("trigger", IPL)["payload"] | {"city": "Mumbai"}}, 2)
    plan = decision(tick(state, IPL), IPL).plan

    with pytest.raises(ValueError, match="no_action"):
        tick_action(plan, "conv_x")


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #

ALL_SEED = [t for t, _ in load_seed_dataset()["trigger"]] if DATASET_DIR.is_dir() else []


def test_same_state_and_time_give_the_same_tick() -> None:
    assert tick(seeded(), *ALL_SEED) == tick(seeded(), *ALL_SEED)


def test_reordered_and_repeated_triggers_give_the_same_tick() -> None:
    assert tick(seeded(), *reversed(ALL_SEED), *ALL_SEED[:5]) == tick(seeded(), *ALL_SEED)


def test_context_push_order_does_not_change_the_tick() -> None:
    clock = FakeClock()
    state = StateContainer.create(clock=clock, monotonic=clock.monotonic)
    for scope, items in reversed(list(load_seed_dataset().items())):
        for context_id, payload in reversed(items):
            push(state, scope, context_id, dict(reversed(list(payload.items()))))

    assert tick(state, *ALL_SEED) == tick(seeded(), *ALL_SEED)


def test_tick_is_identical_across_processes() -> None:
    code = (
        "import json; from tests.test_planner import seeded, tick, ALL_SEED;"
        "print(json.dumps([a.model_dump(mode='json') for a in tick(seeded(), *ALL_SEED).actions]))"
    )
    out = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", code], check=True, capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent
    )

    assert json.loads(out.stdout.strip().splitlines()[-1]) == [a.model_dump(mode="json") for a in tick(seeded(), *ALL_SEED).actions]


def test_repeating_a_tick_on_the_same_state_never_resends() -> None:
    state = seeded()
    first = tick(state, *ALL_SEED)

    again = tick(state, *ALL_SEED)

    assert {a.trigger_id for a in first.actions}.isdisjoint(a.trigger_id for a in again.actions)
    assert {a.suppression_key for a in first.actions}.isdisjoint(a.suppression_key for a in again.actions)


def test_actions_satisfy_the_wire_contract() -> None:
    result = tick(seeded(), *ALL_SEED)

    assert result.actions
    for action in result.actions:
        assert TickAction.model_validate(action.model_dump(mode="json")) == action
        assert action.send_as is (SendAs.MERCHANT_ON_BEHALF if action.customer_id else SendAs.VERA)
