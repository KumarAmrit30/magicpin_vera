"""Phase 2F reply engine: classification, state machine, suppression and history.

Runs the engine directly against a StateContainer (no FastAPI).
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.engine.eligibility import merchant_suppression_key
from app.engine.reply import (
    AUTO_REPLY_WAIT_SECONDS,
    CLOSED_RATIONALE,
    DEFER_WAIT_SECONDS,
    REPLY_PLACEHOLDER_PREFIX,
    ReplyDecision,
    ReplyIntent,
    classify_text,
    handle_reply,
    read_reply,
)
from app.models.enums import ConversationState, CtaType, ReplyAction, TurnRole
from app.models.schemas import EndReply, ReplyRequest, SendReply, WaitReply
from app.state.container import StateContainer
from tests.conftest import FakeClock

R = ReplyIntent
S = ConversationState
M1 = "m_001_drmeera_dentist_delhi"
M2 = "m_002_bharat_dentist_mumbai"
C1 = "c_001_priya_for_m001"
AT = datetime(2026, 4, 26, 10, 42, tzinfo=UTC)

AUTO = "Thank you for contacting us! Our team will respond shortly."
COMMIT = "Ok lets do it. Whats next?"
HOSTILE = "Stop messaging me. This is useless spam."
OPT_OUT = "Not interested. Stop messaging me."


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def state() -> StateContainer:
    clock = FakeClock()
    return StateContainer.create(clock=clock, monotonic=clock.monotonic)


def request(message: str, conversation_id: str = "conv_1", **overrides: Any) -> ReplyRequest:
    fields: dict[str, Any] = {
        "conversation_id": conversation_id,
        "merchant_id": M1,
        "customer_id": None,
        "from_role": "merchant",
        "message": message,
        "received_at": AT,
        "turn_number": 2,
    }
    return ReplyRequest(**(fields | overrides))


def reply(state: StateContainer, message: str, conversation_id: str = "conv_1", **overrides: Any) -> ReplyDecision:
    return handle_reply(state, request(message, conversation_id, **overrides))


def opened(
    state: StateContainer,
    conversation_id: str = "conv_1",
    *,
    merchant_id: str = M1,
    customer_id: str | None = None,
    status: ConversationState = S.NEW,
) -> None:
    """A conversation as a tick leaves it: trigger set and one Vera turn."""
    state.conversation_store.create(
        conversation_id, merchant_id=merchant_id, customer_id=customer_id, trigger_id="trg_x", state=status
    )
    state.conversation_store.append_message(conversation_id, role=TurnRole.VERA, body="[uncomposed] opener", sent_at=AT)


def conversation(state: StateContainer, conversation_id: str = "conv_1"):
    return state.conversation_store.get(conversation_id)


def roles(state: StateContainer, conversation_id: str = "conv_1") -> list[TurnRole]:
    return [t.role for t in conversation(state, conversation_id).turns]


# --------------------------------------------------------------------------- #
# Classification (package messages first)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("message", "intent"),
    [
        # api-call-examples 2.4-2.7, 4.1-4.3; challenge-brief §9; judge_simulator.py
        ("Yes please send the abstract. Also draft the patient WhatsApp.", R.AFFIRMATIVE),
        ("Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly.", R.AUTO_REPLY),
        (AUTO, R.AUTO_REPLY),
        ("Aapki jaankari ke liye bahut-bahut shukriya. Main aapki yeh sabhi baatein aur sujhaav hamari team tak "
         "pahuncha deti hoon.", R.AUTO_REPLY),
        ("Aapki madad ke liye shukriya, lekin main ek automated assistant hoon...", R.AUTO_REPLY),
        (OPT_OUT, R.OPT_OUT),
        ("Ok, let's do it. What's next?", R.AFFIRMATIVE),
        (COMMIT, R.AFFIRMATIVE),
        ("Why are you bothering me. This is useless. Stop sending these.", R.HOSTILE),
        (HOSTILE, R.HOSTILE),
        ("Can you also help me file my GST?", R.OFF_TOPIC),
        ("Mujhe magicpin judrna hai.", R.AFFIRMATIVE),
        # brief §12.2 and the Phase 2F spec
        ("yes I want to join", R.AFFIRMATIVE),
        ("go ahead", R.AFFIRMATIVE),
        ("yes, show me", R.AFFIRMATIVE),
        ("what would that look like?", R.QUESTION),
        ("No, not interested right now.", R.NOT_INTERESTED),
        ("Stop messaging me.", R.OPT_OUT),
        # general coverage
        ("STOP", R.OPT_OUT),
        ("please unsubscribe me", R.OPT_OUT),
        ("no", R.NOT_INTERESTED),
        ("No thanks", R.NOT_INTERESTED),
        ("No problem, go ahead", R.AFFIRMATIVE),
        ("I don't want to miss this, go ahead", R.AFFIRMATIVE),
        ("busy right now, message me later", R.DEFERRAL),
        ("I'll get back to you", R.DEFERRAL),
        ("ok but later", R.DEFERRAL),
        ("Too expensive for us", R.OBJECTION),
        ("ok but it's too costly", R.OBJECTION),
        ("How long does it take?", R.QUESTION),
        ("kitna time lagega", R.QUESTION),
        ("1", R.AFFIRMATIVE),
        ("hmm", R.UNCLEAR),
        ("", R.UNCLEAR),
    ],
)
def test_classify_text(message: str, intent: ReplyIntent) -> None:
    assert classify_text(message)[0] is intent


def test_classify_text_ignores_case_apostrophes_and_spacing() -> None:
    assert classify_text("  OK,   LET’S   DO IT  ") == classify_text("ok, lets do it")


def test_read_reply_counts_trailing_auto_replies() -> None:
    assert read_reply([AUTO]).auto_reply_streak == 1
    assert read_reply([AUTO, AUTO, AUTO]).auto_reply_streak == 3
    assert read_reply([AUTO, "yes", AUTO]).auto_reply_streak == 1
    assert read_reply([AUTO, "yes"]) == read_reply(["yes"])


def test_read_reply_verbatim_repeat_is_an_auto_reply_retroactively() -> None:
    canned = "Welcome to Studio11 Salon. Timings 10am-9pm."

    assert read_reply([canned, canned]).intent is R.UNCLEAR
    reading = read_reply([canned, canned, canned])
    assert (reading.intent, reading.auto_reply_streak) == (R.AUTO_REPLY, 3)
    assert reading.cue == "same message 3x verbatim"


def test_read_reply_verbatim_rule_keeps_explicit_meanings() -> None:
    assert read_reply(["yes"] * 3).intent is R.AFFIRMATIVE
    assert read_reply(["STOP"] * 3).intent is R.OPT_OUT


def test_read_reply_requires_the_latest_message() -> None:
    with pytest.raises(ValueError):
        read_reply([])


# --------------------------------------------------------------------------- #
# Basic conversation (tests 1-6)
# --------------------------------------------------------------------------- #


def test_reply_to_active_conversation_continues_it(state: StateContainer) -> None:
    opened(state)

    decision = reply(state, "How long does setup take?")

    assert isinstance(decision.response, SendReply)
    assert conversation(state).state is S.QUALIFYING
    assert roles(state) == [TurnRole.VERA, TurnRole.MERCHANT, TurnRole.VERA]
    assert conversation(state).turns[-1].body == decision.response.body


@pytest.mark.parametrize("terminal", [S.ENDED, S.COMPLETED])
@pytest.mark.parametrize("message", [COMMIT, HOSTILE, AUTO, "hmm"])
def test_reply_to_closed_conversation_ends_and_changes_nothing_else(
    state: StateContainer, terminal: ConversationState, message: str
) -> None:
    opened(state, status=terminal)

    decision = reply(state, message)

    assert decision.response == EndReply(rationale=CLOSED_RATIONALE)
    assert conversation(state).state is terminal
    assert roles(state) == [TurnRole.VERA, TurnRole.MERCHANT]
    assert len(state.suppression_store) == 0


def test_unknown_conversation_is_created_for_the_request_participants_only(state: StateContainer) -> None:
    opened(state, "conv_other")

    reply(state, COMMIT, "conv_new", merchant_id=M2)

    created = conversation(state, "conv_new")
    assert (created.merchant_id, created.customer_id, created.trigger_id) == (M2, None, None)
    assert roles(state, "conv_new") == [TurnRole.MERCHANT, TurnRole.VERA]
    assert roles(state, "conv_other") == [TurnRole.VERA]
    assert conversation(state, "conv_other").state is S.NEW


def test_unknown_conversation_without_ids_gets_no_owner_until_one_is_supplied(state: StateContainer) -> None:
    reply(state, "hmm", merchant_id=None)
    assert conversation(state).merchant_id is None

    reply(state, "hmm", turn_number=3)
    assert conversation(state).merchant_id == M1


@pytest.mark.parametrize(
    ("owner", "overrides"),
    [
        ({"merchant_id": M1}, {"merchant_id": M2}),
        ({"merchant_id": M1}, {"customer_id": C1, "from_role": "customer"}),
        ({"merchant_id": M1, "customer_id": C1}, {"customer_id": "c_999_other", "from_role": "customer"}),
    ],
)
def test_ownership_conflict_is_refused_without_mutation(
    state: StateContainer, owner: dict[str, str], overrides: dict[str, Any]
) -> None:
    opened(state, **owner)
    before = conversation(state)

    decision = reply(state, HOSTILE, **overrides)

    assert isinstance(decision.response, EndReply)
    assert "does not belong" in decision.response.rationale
    assert decision.reading is None
    assert conversation(state) == before
    assert len(state.suppression_store) == 0


def test_customer_reply_without_customer_id_is_accepted_on_customer_thread(state: StateContainer) -> None:
    opened(state, customer_id=C1)

    decision = reply(state, "1", from_role="customer")

    assert decision.response.action is ReplyAction.SEND
    assert conversation(state).customer_id == C1
    assert roles(state) == [TurnRole.VERA, TurnRole.CUSTOMER, TurnRole.VERA]


def test_history_keeps_every_turn_in_order_with_sender_and_timestamp(state: StateContainer) -> None:
    opened(state)
    script = [("How much?", 2), ("later", 3), (COMMIT, 4), ("When will it be live?", 5)]

    for i, (message, turn) in enumerate(script):
        reply(state, message, turn_number=turn, received_at=AT + timedelta(minutes=i))

    turns = conversation(state).turns
    inbound = [t for t in turns if t.role is TurnRole.MERCHANT]
    assert [(t.body, t.turn_number, t.sent_at) for t in inbound] == [
        (message, turn, AT + timedelta(minutes=i)) for i, (message, turn) in enumerate(script)
    ]
    assert [t.role for t in turns] == [
        TurnRole.VERA, TurnRole.MERCHANT, TurnRole.VERA, TurnRole.MERCHANT, TurnRole.MERCHANT, TurnRole.VERA,
        TurnRole.MERCHANT, TurnRole.VERA,
    ]
    assert turns[0].body == "[uncomposed] opener"


def run_script(script: list[tuple[str, str, str]]) -> tuple[list[Any], dict[str, Any]]:
    clock = FakeClock()
    state = StateContainer.create(clock=clock, monotonic=clock.monotonic)
    responses = []
    for i, (conversation_id, merchant_id, message) in enumerate(script):
        clock.advance(1)
        decision = reply(state, message, conversation_id, merchant_id=merchant_id, turn_number=i + 2)
        responses.append(decision.response.model_dump(mode="json"))
    dumps = {cid: state.conversation_store.get(cid).model_dump(mode="json") for cid, _, _ in script}
    return responses, dumps


REPLAY = [
    ("conv_a", M1, "How much?"), ("conv_b", M2, AUTO), ("conv_a", M1, COMMIT), ("conv_b", M2, AUTO),
    ("conv_c", M1, HOSTILE), ("conv_b", M2, AUTO), ("conv_a", M1, "thanks"),
]


def test_repeated_reply_sequence_is_deterministic() -> None:
    assert run_script(REPLAY) == run_script(REPLAY)


def test_independent_senders_are_unaffected_by_interleaving() -> None:
    by_sender = sorted(REPLAY, key=lambda item: item[1])
    responses, dumps = run_script(REPLAY)
    reordered_responses, reordered_dumps = run_script(by_sender)

    def per_conversation(script, results):
        grouped: dict[str, list] = {}
        for (cid, _, _), result in zip(script, results, strict=True):
            grouped.setdefault(cid, []).append({k: v for k, v in result.items() if k != "body"})
        return grouped

    assert per_conversation(REPLAY, responses) == per_conversation(by_sender, reordered_responses)
    assert {cid: d["state"] for cid, d in dumps.items()} == {cid: d["state"] for cid, d in reordered_dumps.items()}


# --------------------------------------------------------------------------- #
# Intent (tests 7-14)
# --------------------------------------------------------------------------- #


def test_affirmative_switches_to_action_mode(state: StateContainer) -> None:
    opened(state)

    decision = reply(state, COMMIT)

    assert decision.response.action is ReplyAction.SEND
    assert decision.response.cta is CtaType.BINARY_CONFIRM_CANCEL
    assert REPLY_PLACEHOLDER_PREFIX not in decision.response.body
    assert "Reply CONFIRM" in decision.response.body and "trg_x" not in decision.response.body
    assert conversation(state).state is S.COMMITTED
    assert len(state.conversation_store) == 1


def test_action_mode_body_has_no_qualifying_question(state: StateContainer) -> None:
    body = reply(state, COMMIT).response.body.lower()

    assert any(w in body for w in ["done", "sending", "draft", "here", "confirm", "proceed", "next"])
    assert not any(w in body for w in ["would you", "do you", "can you tell", "what if", "how about"])


@pytest.mark.parametrize("message", ["No, not interested right now.", "no thanks", "no"])
def test_negative_ends_conversation_without_suppression(state: StateContainer, message: str) -> None:
    opened(state)

    decision = reply(state, message)

    assert isinstance(decision.response, EndReply)
    assert decision.reading.intent is R.NOT_INTERESTED
    assert conversation(state).state is S.ENDED
    assert len(state.suppression_store) == 0


def test_deferral_waits_thirty_minutes_and_can_resume(state: StateContainer) -> None:
    opened(state)

    decision = reply(state, "busy now, later")
    assert decision.response == WaitReply(wait_seconds=DEFER_WAIT_SECONDS, rationale=decision.response.rationale)
    assert conversation(state).state is S.WAITING
    assert roles(state) == [TurnRole.VERA, TurnRole.MERCHANT]

    assert reply(state, "ok go ahead", turn_number=3).response.action is ReplyAction.SEND
    assert conversation(state).state is S.COMMITTED


@pytest.mark.parametrize(
    ("message", "intent", "cta"),
    [
        ("How long does setup take?", R.QUESTION, CtaType.OPEN_ENDED),
        ("Too expensive for us", R.OBJECTION, CtaType.OPEN_ENDED),
        ("hmm", R.UNCLEAR, CtaType.BINARY_YES_NO),
    ],
)
def test_engaged_replies_get_a_structured_send(
    state: StateContainer, message: str, intent: ReplyIntent, cta: CtaType
) -> None:
    opened(state)

    decision = reply(state, message)

    assert decision.reading.intent is intent
    assert (decision.response.action, decision.response.cta) == (ReplyAction.SEND, cta)
    assert REPLY_PLACEHOLDER_PREFIX not in decision.response.body
    assert decision.response.body.endswith("?") and decision.response.body.count("?") == 1
    assert conversation(state).state is S.QUALIFYING


def test_off_topic_redirects_to_the_original_trigger_without_new_outreach(state: StateContainer) -> None:
    opened(state)

    decision = reply(state, "Can you also help me file my GST?")

    assert decision.reading.intent is R.OFF_TOPIC
    assert decision.response.cta is CtaType.OPEN_ENDED
    assert decision.response.body.startswith("That's outside my scope here.")
    assert "trg_x" not in decision.response.body
    assert conversation(state).trigger_id == "trg_x"
    assert (len(state.conversation_store), len(state.suppression_store)) == (1, 0)


def test_auto_reply_hell_in_one_conversation(state: StateContainer) -> None:
    """api-call-examples 4.1: prompt once, wait 24h, then close; replies after closing get ``end``."""
    opened(state)

    decisions = [reply(state, AUTO, turn_number=t) for t in (2, 3, 4, 5)]

    first, second, third, fourth = (d.response for d in decisions)
    assert (first.action, first.cta) == (ReplyAction.SEND, CtaType.BINARY_YES_NO)
    assert second == WaitReply(wait_seconds=AUTO_REPLY_WAIT_SECONDS, rationale=second.rationale)
    assert isinstance(third, EndReply) and "3x in a row" in third.rationale
    assert fourth == EndReply(rationale=CLOSED_RATIONALE)
    assert [d.state for d in decisions] == [S.NEW, S.WAITING, S.ENDED, S.ENDED]
    assert roles(state).count(TurnRole.VERA) == 2
    assert len(state.suppression_store) == 0


def test_auto_reply_streak_follows_the_sender_across_conversations(state: StateContainer) -> None:
    """judge_simulator auto_reply_hell: the same canned text on conv_auto_1..4 for one merchant."""
    actions = [reply(state, AUTO, f"conv_auto_{i}", turn_number=i + 1).response.action for i in range(1, 5)]

    assert actions == [ReplyAction.SEND, ReplyAction.WAIT, ReplyAction.END, ReplyAction.END]
    assert [conversation(state, f"conv_auto_{i}").state for i in range(1, 5)] == [S.NEW, S.WAITING, S.ENDED, S.ENDED]


def test_auto_reply_streak_is_per_sender_and_reset_by_a_real_reply(state: StateContainer) -> None:
    reply(state, AUTO, "conv_1")
    reply(state, AUTO, "conv_2", merchant_id=M2)
    assert reply(state, AUTO, "conv_3", merchant_id=M2).response.action is ReplyAction.WAIT

    reply(state, "How much?", "conv_1", turn_number=3)
    assert reply(state, AUTO, "conv_1", turn_number=4).response.action is ReplyAction.SEND


def test_auto_reply_is_neither_interest_nor_rejection(state: StateContainer) -> None:
    opened(state, status=S.QUALIFYING)

    reply(state, AUTO)

    assert conversation(state).state is S.QUALIFYING


def test_hostile_merchant_ends_and_suppresses_the_merchant_without_expiry(state: StateContainer) -> None:
    """api-call-examples 4.3 suppresses all triggers for the merchant; its "30 days" is illustrative only."""
    opened(state)

    decision = reply(state, HOSTILE)

    key = merchant_suppression_key(M1)
    assert isinstance(decision.response, EndReply)
    assert key in decision.response.rationale and "30 days" not in decision.response.rationale
    assert conversation(state).state is S.ENDED
    assert state.suppression_store.peek(key, AT).expires_at is None
    assert state.suppression_store.peek(key, AT + timedelta(days=3650)) is not None
    assert len(state.suppression_store) == 1


def test_hostile_customer_ends_without_merchant_suppression(state: StateContainer) -> None:
    opened(state, customer_id=C1)

    decision = reply(state, HOSTILE, from_role="customer", customer_id=C1)

    assert isinstance(decision.response, EndReply)
    assert conversation(state).state is S.ENDED
    assert len(state.suppression_store) == 0


def test_hostile_with_unknown_merchant_writes_nothing(state: StateContainer) -> None:
    decision = reply(state, HOSTILE, merchant_id=None)

    assert isinstance(decision.response, EndReply)
    assert len(state.suppression_store) == 0


@pytest.mark.parametrize("message", [OPT_OUT, "STOP", "unsubscribe"])
def test_explicit_opt_out_ends_this_conversation_only(state: StateContainer, message: str) -> None:
    """api-call-examples 2.6: close and suppress this conversation_id; no merchant-wide key."""
    opened(state)
    opened(state, "conv_2")

    decision = reply(state, message)

    assert isinstance(decision.response, EndReply)
    assert "no further messages on this conversation_id" in decision.response.rationale
    assert conversation(state).state is S.ENDED
    assert conversation(state, "conv_2").state is S.NEW
    assert len(state.suppression_store) == 0
    assert reply(state, "yes", turn_number=3).response == EndReply(rationale=CLOSED_RATIONALE)


# --------------------------------------------------------------------------- #
# State transitions (tests 15-19)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("start", "message", "end"),
    [
        (S.NEW, "How much?", S.QUALIFYING),
        (S.NEW, COMMIT, S.COMMITTED),
        (S.NEW, "later", S.WAITING),
        (S.NEW, AUTO, S.NEW),
        (S.NEW, "no", S.ENDED),
        (S.QUALIFYING, "Too costly", S.QUALIFYING),
        (S.QUALIFYING, COMMIT, S.COMMITTED),
        (S.COMMITTED, "How long does it take?", S.COMMITTED),
        (S.COMMITTED, "file my GST too?", S.COMMITTED),
        (S.COMMITTED, COMMIT, S.COMMITTED),
        (S.COMMITTED, "later", S.WAITING),
        (S.WAITING, "How much?", S.QUALIFYING),
        (S.WAITING, OPT_OUT, S.ENDED),
        (S.QUALIFYING, HOSTILE, S.ENDED),
        (S.ENDED, COMMIT, S.ENDED),
        (S.COMPLETED, "How much?", S.COMPLETED),
    ],
)
def test_state_transitions(state: StateContainer, start: ConversationState, message: str, end: ConversationState) -> None:
    opened(state, status=start)

    decision = reply(state, message)

    assert decision.state is end
    assert conversation(state).state is end


def test_reply_never_sets_completed(state: StateContainer) -> None:
    opened(state)
    for turn, message in enumerate([COMMIT, "confirm", "done", "yes"], start=2):
        reply(state, message, turn_number=turn)

    assert conversation(state).state is S.COMMITTED


# --------------------------------------------------------------------------- #
# Suppression (tests 20-25; future ticks in test_reply_dataset.py)
# --------------------------------------------------------------------------- #


def test_suppression_key_and_expiry_are_deterministic() -> None:
    def hostile_record():
        state = StateContainer.create(clock=FakeClock())
        reply(state, HOSTILE)
        return state.suppression_store.peek(merchant_suppression_key(M1), AT)

    assert hostile_record() == hostile_record()
    assert hostile_record().key == f"suppress:merchant:{M1}"


def test_repeated_hostility_on_an_ended_conversation_writes_nothing_new(state: StateContainer) -> None:
    reply(state, HOSTILE)
    record = state.suppression_store.peek(merchant_suppression_key(M1), AT)

    reply(state, HOSTILE, turn_number=3, received_at=AT + timedelta(days=5))

    assert state.suppression_store.peek(merchant_suppression_key(M1), AT) == record


def test_hostility_on_another_conversation_keeps_the_first_record(state: StateContainer) -> None:
    key = merchant_suppression_key(M1)
    reply(state, HOSTILE, "conv_1")
    record = state.suppression_store.peek(key, AT)

    reply(state, HOSTILE, "conv_2", received_at=AT + timedelta(days=10))

    assert state.suppression_store.peek(key, AT) == record
    assert "conv_1" in record.reason


def test_hostility_makes_an_expiring_merchant_suppression_indefinite(state: StateContainer) -> None:
    key = merchant_suppression_key(M1)
    state.suppression_store.suppress(key, reason="operator", expires_at=AT + timedelta(days=1))

    reply(state, HOSTILE)

    assert state.suppression_store.peek(key, AT).expires_at is None


def test_hostility_keeps_a_permanent_merchant_suppression(state: StateContainer) -> None:
    key = merchant_suppression_key(M1)
    state.suppression_store.suppress(key, reason="operator")

    reply(state, HOSTILE)

    assert state.suppression_store.peek(key, AT).expires_at is None
    assert state.suppression_store.peek(key, AT).reason == "operator"


@pytest.mark.parametrize("message", ["no", OPT_OUT, "later", AUTO, "Too expensive", "file my GST?", COMMIT])
def test_only_hostility_writes_suppression(state: StateContainer, message: str) -> None:
    for turn in range(2, 6):
        reply(state, message, turn_number=turn)

    assert len(state.suppression_store) == 0


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #


def test_reply_does_not_run_candidate_generation_or_the_planner(state: StateContainer, monkeypatch) -> None:
    import app.engine.planner as planner

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("reply must not plan")

    monkeypatch.setattr(planner, "generate_candidates", forbidden)
    monkeypatch.setattr(planner, "plan_tick", forbidden)
    opened(state)

    for turn, message in enumerate(["file my GST?", "How much?", COMMIT, AUTO, "later", HOSTILE], start=2):
        reply(state, message, turn_number=turn)

    assert len(state.conversation_store) == 1


def test_vera_turn_is_recorded_only_for_send(state: StateContainer) -> None:
    opened(state)
    reply(state, "later")
    reply(state, "How much?", turn_number=3)
    reply(state, "no", turn_number=4)

    assert roles(state) == [TurnRole.VERA, TurnRole.MERCHANT, TurnRole.MERCHANT, TurnRole.VERA, TurnRole.MERCHANT]


def test_send_bodies_are_unique_within_a_conversation(state: StateContainer) -> None:
    opened(state)
    questions = [("How much?", 2), ("How long?", 3), ("Which slots?", 4)]
    bodies = [reply(state, message, turn_number=t).response.body for message, t in questions]

    assert len(set(bodies)) == 3
