from datetime import UTC, datetime

import pytest

from app.models.enums import ConversationState, TurnRole
from app.state.conversation_store import ConversationExistsError, ConversationNotFoundError, ConversationStore
from tests.conftest import T0, FakeClock

SENT = datetime(2026, 4, 26, 10, 42, tzinfo=UTC)


@pytest.fixture
def store(clock: FakeClock) -> ConversationStore:
    return ConversationStore(clock)


def test_create_initialises_conversation(store: ConversationStore) -> None:
    conversation = store.create("conv_1", merchant_id="m_001", trigger_id="trg_001")

    assert conversation.conversation_id == "conv_1"
    assert conversation.merchant_id == "m_001"
    assert conversation.customer_id is None
    assert conversation.trigger_id == "trg_001"
    assert conversation.state is ConversationState.NEW
    assert conversation.turns == []
    assert conversation.created_at == conversation.updated_at == T0


def test_create_duplicate_id_raises(store: ConversationStore) -> None:
    store.create("conv_1", merchant_id="m_001")

    with pytest.raises(ConversationExistsError):
        store.create("conv_1", merchant_id="m_002")


def test_get_retrieves_and_missing_returns_none(store: ConversationStore) -> None:
    store.create("conv_1", merchant_id="m_001", customer_id="c_001")

    assert store.get("conv_1").customer_id == "c_001"
    assert store.get("unknown") is None
    assert store.exists("conv_1")
    assert not store.exists("unknown")


def test_append_message_records_turn(store: ConversationStore, clock: FakeClock) -> None:
    store.create("conv_1", merchant_id="m_001")
    clock.advance(5)

    conversation = store.append_message("conv_1", role=TurnRole.MERCHANT, body="Yes please", sent_at=SENT, turn_number=2)

    [turn] = conversation.turns
    assert turn.role is TurnRole.MERCHANT
    assert turn.body == "Yes please"
    assert turn.sent_at == SENT
    assert turn.recorded_at == clock.now
    assert turn.turn_number == 2


def test_turns_preserve_append_order(store: ConversationStore) -> None:
    store.create("conv_1", merchant_id="m_001")
    messages = [(TurnRole.VERA, "hello"), (TurnRole.MERCHANT, "hi"), (TurnRole.VERA, "draft?"), (TurnRole.MERCHANT, "ok")]

    for i, (role, body) in enumerate(messages, start=1):
        store.append_message("conv_1", role=role, body=body, sent_at=SENT, turn_number=i)

    turns = store.get("conv_1").turns
    assert [(t.role, t.body, t.turn_number) for t in turns] == [(r, b, i) for i, (r, b) in enumerate(messages, start=1)]


def test_set_state_transitions(store: ConversationStore) -> None:
    store.create("conv_1", merchant_id="m_001")

    for state in (ConversationState.QUALIFYING, ConversationState.COMMITTED, ConversationState.COMPLETED):
        assert store.set_state("conv_1", state).state is state

    assert store.get("conv_1").state is ConversationState.COMPLETED


def test_timestamps_advance_on_every_mutation(store: ConversationStore, clock: FakeClock) -> None:
    store.create("conv_1", merchant_id="m_001")

    clock.advance(10)
    after_append = store.append_message("conv_1", role=TurnRole.MERCHANT, body="hi", sent_at=SENT)
    clock.advance(10)
    after_state = store.set_state("conv_1", ConversationState.WAITING)
    clock.advance(10)
    after_update = store.update("conv_1", customer_id="c_001")

    assert after_append.updated_at == T0.replace(second=10)
    assert after_state.updated_at == T0.replace(second=20)
    assert after_update.updated_at == T0.replace(second=30)
    assert after_update.created_at == T0


def test_update_sets_given_fields_only(store: ConversationStore) -> None:
    store.create("conv_1")

    store.update("conv_1", merchant_id="m_001")
    conversation = store.update("conv_1", trigger_id="trg_001")

    assert conversation.merchant_id == "m_001"
    assert conversation.trigger_id == "trg_001"
    assert conversation.customer_id is None


def test_get_or_create(store: ConversationStore) -> None:
    created, was_created = store.get_or_create("conv_1", merchant_id="m_001")
    again, was_created_again = store.get_or_create("conv_1", merchant_id="m_999")

    assert was_created and not was_created_again
    assert created.merchant_id == again.merchant_id == "m_001"
    assert len(store) == 1


def test_returned_conversations_are_snapshots(store: ConversationStore) -> None:
    store.create("conv_1", merchant_id="m_001")
    snapshot = store.append_message("conv_1", role=TurnRole.VERA, body="x", sent_at=SENT)

    snapshot.state = ConversationState.ENDED
    snapshot.merchant_id = "m_999"
    snapshot.turns.clear()

    stored = store.get("conv_1")
    assert stored.state is ConversationState.NEW
    assert stored.merchant_id == "m_001"
    assert len(stored.turns) == 1


@pytest.mark.parametrize(
    "operation",
    [
        lambda s: s.update("missing", merchant_id="m"),
        lambda s: s.append_message("missing", role=TurnRole.MERCHANT, body="x", sent_at=SENT),
        lambda s: s.set_state("missing", ConversationState.ENDED),
    ],
)
def test_operations_on_unknown_conversation_raise(store: ConversationStore, operation) -> None:
    with pytest.raises(ConversationNotFoundError):
        operation(store)


def test_clear_removes_all_conversations(store: ConversationStore) -> None:
    store.create("conv_1")
    store.create("conv_2")

    store.clear()

    assert len(store) == 0
    assert store.get("conv_1") is None


@pytest.mark.parametrize(
    ("state", "terminal"),
    [(s, s in (ConversationState.COMPLETED, ConversationState.ENDED)) for s in ConversationState],
)
def test_terminal_states(state: ConversationState, terminal: bool) -> None:
    assert state.is_terminal is terminal
