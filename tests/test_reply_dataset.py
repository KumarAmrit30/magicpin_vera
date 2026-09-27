"""Phase 2F replay on the seed dataset: tick -> conversation -> replies -> next tick.

Engine-level replays run the real Phase 2E planner and the reply engine on one
StateContainer. The judge-simulator scenarios at the end go through the HTTP
API with the simulator's exact payloads and pass criteria.
"""

import functools
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.engine.candidates import generate_candidates
from app.engine.eligibility import EligibilityReasonCode, evaluate_candidates, merchant_suppression_key
from app.engine.planner import TriggerOutcome, plan_tick
from app.engine.reply import ReplyDecision, handle_reply
from app.main import create_app
from app.models.enums import ConversationState, CtaType, ReplyAction, SendAs, TurnRole
from app.models.schemas import EndReply, ReplyRequest, TickAction
from app.state.container import StateContainer
from app.state.suppression_store import SuppressionStore
from tests.conftest import DATASET_DIR, SEED_NOW, FakeClock, context_body, load_seed_dataset, requires_dataset, seed_context
from tests.test_planner import push, seed_payload, seeded, tick
from tests.test_planner_dataset import loaded

pytestmark = requires_dataset

C = EligibilityReasonCode
S = ConversationState
DIGEST = "trg_001_research_digest_dentists"
COMPLIANCE = "trg_002_compliance_dci_radiograph"
RECALL = "trg_003_recall_due_priya"
WEBINAR = "trg_022_cde_webinar_dentists"
M001 = "m_001_drmeera_dentist_delhi"
REPLY_AT = SEED_NOW + timedelta(hours=1)

AUTO = "Thank you for contacting us! Our team will respond shortly."
COMMIT = "Ok lets do it. Whats next?"
HOSTILE = "Stop messaging me. This is useless spam."
OPT_OUT = "Not interested. Stop messaging me."
ACTIONING = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def opened(trigger_id: str) -> tuple[StateContainer, TickAction]:
    state = seeded()
    [action] = tick(state, trigger_id).actions
    return state, action


def answer(state: StateContainer, action: TickAction, message: str, turn: int = 2, **overrides: Any) -> ReplyDecision:
    """A reply from the recipient of ``action`` on its conversation."""
    customer = action.send_as is SendAs.MERCHANT_ON_BEHALF
    fields = {
        "conversation_id": action.conversation_id,
        "merchant_id": action.merchant_id,
        "customer_id": action.customer_id,
        "from_role": "customer" if customer else "merchant",
        "message": message,
        "received_at": REPLY_AT + timedelta(minutes=turn),
        "turn_number": turn,
    }
    return handle_reply(state, ReplyRequest(**(fields | overrides)))


@functools.cache
def emitted_seed_triggers() -> tuple[str, ...]:
    ids = [trigger_id for trigger_id, _ in load_seed_dataset()["trigger"]]
    return tuple(t for t in ids if tick(seeded(), t).actions)


def test_seed_replays_cover_every_seed_trigger() -> None:
    assert len(emitted_seed_triggers()) == len(load_seed_dataset()["trigger"]) == 25


# --------------------------------------------------------------------------- #
# Replay scenarios on every conversation the planner opens
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("trigger_id", emitted_seed_triggers())
def test_intent_transition_replay(trigger_id: str) -> None:
    """Replay 2 (testing brief §4): two qualifying turns, then "let's do it" -> action mode."""
    state, action = opened(trigger_id)
    keys_after_tick = len(state.suppression_store)

    answer(state, action, "What would that look like?", turn=2)
    answer(state, action, "How long does it take?", turn=3)
    decision = answer(state, action, COMMIT, turn=4)

    body = decision.response.body.lower()
    assert (decision.response.action, decision.response.cta) == (ReplyAction.SEND, CtaType.BINARY_CONFIRM_CANCEL)
    assert any(w in body for w in ACTIONING) and not any(w in body for w in QUALIFYING)
    assert f"trigger={trigger_id}" in decision.response.body
    conversation = state.conversation_store.get(action.conversation_id)
    assert conversation.state is S.COMMITTED
    assert conversation.turns[0].body == action.body
    assert len(conversation.turns) == 7
    assert (len(state.conversation_store), len(state.suppression_store)) == (1, keys_after_tick)


@pytest.mark.parametrize("trigger_id", emitted_seed_triggers())
def test_auto_reply_hell_replay(trigger_id: str) -> None:
    """Replay 1: the same canned text 4 times -> prompt, wait, end, end."""
    state, action = opened(trigger_id)

    actions = [answer(state, action, AUTO, turn=t).response.action for t in (2, 3, 4, 5)]

    assert actions == [ReplyAction.SEND, ReplyAction.WAIT, ReplyAction.END, ReplyAction.END]
    assert state.conversation_store.get(action.conversation_id).state is S.ENDED


@pytest.mark.parametrize("trigger_id", emitted_seed_triggers())
def test_hostile_then_off_topic_replay(trigger_id: str) -> None:
    """Replay 3: abuse, then an unrelated question. Closed stays closed; only merchant hostility suppresses."""
    state, action = opened(trigger_id)
    keys_after_tick = len(state.suppression_store)

    hostile = answer(state, action, HOSTILE, turn=2)
    off_topic = answer(state, action, "Can you also help me file my GST?", turn=3)

    assert isinstance(hostile.response, EndReply)
    assert isinstance(off_topic.response, EndReply)
    assert state.conversation_store.get(action.conversation_id).state is S.ENDED
    merchant_key = state.suppression_store.peek(merchant_suppression_key(action.merchant_id), REPLY_AT)
    if action.send_as is SendAs.VERA:
        assert merchant_key.expires_at is None
        assert len(state.suppression_store) == keys_after_tick + 1
    else:
        assert merchant_key is None
        assert len(state.suppression_store) == keys_after_tick


@pytest.mark.parametrize("trigger_id", emitted_seed_triggers())
def test_off_topic_on_live_conversation_stays_on_trigger(trigger_id: str) -> None:
    state, action = opened(trigger_id)

    decision = answer(state, action, "Can you also help me file my GST?")

    assert decision.response.cta is CtaType.OPEN_ENDED
    assert f"trigger={trigger_id}" in decision.response.body
    assert state.conversation_store.get(action.conversation_id).trigger_id == trigger_id
    assert len(state.conversation_store) == 1


def test_canonical_pairs_replay(expanded: dict[str, Any]) -> None:
    """Every canonical test pair the planner emits: commitment -> action mode, hostility -> closed."""
    emitted = 0
    for pair in expanded["pairs"]:
        state = loaded(expanded)
        actions = plan_tick(state, now=SEED_NOW, available_triggers=[pair["trigger_id"]]).actions
        if not actions:
            continue
        emitted += 1
        [action] = actions
        commit = answer(state, action, COMMIT, turn=2)
        assert commit.response.cta is CtaType.BINARY_CONFIRM_CANCEL, pair
        assert isinstance(answer(state, action, HOSTILE, turn=3).response, EndReply), pair
        assert answer(state, action, COMMIT, turn=4).response.action is ReplyAction.END, pair
        assert len(state.conversation_store) == 1

    assert (len(expanded["pairs"]), emitted) == (30, 27)


# --------------------------------------------------------------------------- #
# Future ticks observe reply outcomes (test 23)
# --------------------------------------------------------------------------- #


def test_future_tick_observes_merchant_hostility_without_expiry() -> None:
    state, action = opened(DIGEST)
    answer(state, action, HOSTILE)

    blocked = tick(state, COMPLIANCE, WEBINAR, RECALL, "trg_010_ipl_match_delhi")

    for trigger_id in (COMPLIANCE, WEBINAR, RECALL):
        decision = next(d for d in blocked.decisions if d.trigger_id == trigger_id)
        assert decision.outcome is TriggerOutcome.NO_ACTION
        assert C.MERCHANT_SUPPRESSED in decision.reason_codes
    assert [a.trigger_id for a in blocked.actions] == ["trg_010_ipl_match_delhi"]

    for days in (31, 200):
        later = tick(state, COMPLIANCE, now=REPLY_AT + timedelta(days=days))
        assert later.actions == ()
        assert C.MERCHANT_SUPPRESSED in later.decisions[0].reason_codes


def test_opt_out_closes_only_its_conversation_and_later_ticks_open_fresh_ones() -> None:
    """api-call-examples 2.6 suppresses the conversation_id, not the merchant."""
    state, action = opened(DIGEST)
    answer(state, action, OPT_OUT)

    later = tick(state, COMPLIANCE)

    [next_action] = later.actions
    assert next_action.conversation_id != action.conversation_id
    assert state.conversation_store.get(action.conversation_id).state is S.ENDED
    assert state.suppression_store.peek(merchant_suppression_key(M001), SEED_NOW) is None


def test_customer_hostility_does_not_block_merchant_outreach() -> None:
    state, action = opened(RECALL)
    assert action.send_as is SendAs.MERCHANT_ON_BEHALF

    answer(state, action, HOSTILE)

    assert [a.trigger_id for a in tick(state, COMPLIANCE).actions] == [COMPLIANCE]


def test_negative_reply_does_not_block_future_ticks() -> None:
    state, action = opened(DIGEST)
    answer(state, action, "No, not interested right now.")

    assert [a.trigger_id for a in tick(state, COMPLIANCE).actions] == [COMPLIANCE]


# --------------------------------------------------------------------------- #
# Nudge interaction with Phase 2C (tests 26-28)
# --------------------------------------------------------------------------- #


def nudge_blocked(state: StateContainer, conversation_id: str) -> bool:
    """Whether Phase 2C blocks every sendable merchant candidate for the digest on this thread."""
    context = seed_context(DIGEST, conversation=state.conversation_store.get(conversation_id))
    results = evaluate_candidates(generate_candidates(context), context, SuppressionStore(), now=SEED_NOW)
    sendable = [r for r in results if r.candidate.action.value != "no_action"]
    return all(C.NUDGE_LIMIT_REACHED in {reason.code for reason in r.reasons} for r in sendable)


def test_genuine_reply_resets_the_unanswered_nudge_streak() -> None:
    state, action = opened(DIGEST)
    for _ in range(2):
        state.conversation_store.append_message(action.conversation_id, role=TurnRole.VERA, body="nudge", sent_at=SEED_NOW)
    assert nudge_blocked(state, action.conversation_id)

    answer(state, action, "How much does it cost?")

    turns = state.conversation_store.get(action.conversation_id).turns
    assert [t.role for t in turns][-2:] == [TurnRole.MERCHANT, TurnRole.VERA]
    assert not nudge_blocked(state, action.conversation_id)


def test_reply_send_counts_as_one_unanswered_bot_turn() -> None:
    state, action = opened(DIGEST)
    answer(state, action, "How much does it cost?")
    for _ in range(2):
        state.conversation_store.append_message(action.conversation_id, role=TurnRole.VERA, body="nudge", sent_at=SEED_NOW)

    assert nudge_blocked(state, action.conversation_id)


def test_three_unanswered_nudges_in_merchant_history_still_block_ticks() -> None:
    state = seeded()
    merchant = seed_payload("merchant", M001)
    merchant["conversation_history"] += [
        {"ts": f"2026-04-25T1{i}:00:00Z", "from": "vera", "body": f"nudge {i}"} for i in range(3)
    ]
    push(state, "merchant", M001, merchant, version=2)

    result = tick(state, DIGEST)

    assert result.actions == ()
    assert C.NUDGE_LIMIT_REACHED in result.decisions[0].reason_codes


# --------------------------------------------------------------------------- #
# Determinism of a full tick + reply session
# --------------------------------------------------------------------------- #


def session() -> list[Any]:
    state = seeded()
    out: list[Any] = []
    first = tick(state, DIGEST, RECALL, COMPLIANCE)
    out.append([a.model_dump(mode="json") for a in first.actions])
    script = [AUTO, COMMIT, "How long?", HOSTILE, AUTO, AUTO]
    for i, action in enumerate(first.actions):
        for turn, message in enumerate(script[i::3], start=2):
            out.append(answer(state, action, message, turn=turn).response.model_dump(mode="json"))
    out.append([a.model_dump(mode="json") for a in tick(state, WEBINAR, now=SEED_NOW + timedelta(days=1)).actions])
    out.append({cid: c.model_dump(mode="json") for cid in (a.conversation_id for a in first.actions)
                for c in [state.conversation_store.get(cid)]})
    return out


def test_tick_and_reply_session_is_deterministic() -> None:
    assert session() == session()


# --------------------------------------------------------------------------- #
# judge_simulator.py reply scenarios through the HTTP API
# --------------------------------------------------------------------------- #


@pytest.fixture
def warmed() -> TestClient:
    """The simulator's warmup: every category and the first five merchants."""
    clock = FakeClock()
    client = TestClient(create_app(settings=Settings(), state=StateContainer.create(clock=clock, monotonic=clock.monotonic)))
    for slug, payload in load_seed_dataset()["category"]:
        assert client.post("/v1/context", json=context_body("category", slug, 1, payload)).json()["accepted"]
    for merchant_id, payload in load_seed_dataset()["merchant"][:5]:
        assert client.post("/v1/context", json=context_body("merchant", merchant_id, 1, payload)).json()["accepted"]
    return client


def simulator_merchant() -> str:
    merchants = json.loads((DATASET_DIR / "merchants_seed.json").read_text())["merchants"]
    return merchants[0]["merchant_id"]


def simulator_reply(client: TestClient, conversation_id: str, message: str, turn: int) -> dict[str, Any]:
    response = client.post("/v1/reply", json={
        "conversation_id": conversation_id, "merchant_id": simulator_merchant(), "customer_id": None,
        "from_role": "merchant", "message": message,
        "received_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "turn_number": turn,
    })
    assert response.status_code == 200
    return response.json()


def test_simulator_auto_reply_hell_ends(warmed: TestClient) -> None:
    actions = []
    for i in range(1, 5):
        data = simulator_reply(warmed, f"conv_auto_{i}", AUTO, i + 1)
        actions.append(data["action"])
        if data["action"] == "end":
            break

    assert actions == ["send", "wait", "end"]


def test_simulator_intent_transition_switches_to_action(warmed: TestClient) -> None:
    data = simulator_reply(warmed, "conv_intent_1", "Ok lets do it. Whats next?", 2)

    body = data.get("body", "").lower()
    assert data["action"] == "send"
    assert any(w in body for w in ACTIONING) and not any(w in body for w in QUALIFYING)


def test_simulator_hostile_ends(warmed: TestClient) -> None:
    assert simulator_reply(warmed, "conv_hostile", HOSTILE, 2)["action"] == "end"
