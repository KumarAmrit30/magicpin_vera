"""In-memory store of conversations and their ordered turns."""

import logging
import threading
from datetime import datetime

from pydantic import BaseModel, Field

from app.clock import Clock, utc_now
from app.models.enums import ConversationState, TurnRole

logger = logging.getLogger(__name__)


class ConversationNotFoundError(KeyError):
    """Raised when an operation targets an unknown ``conversation_id``."""


class ConversationExistsError(ValueError):
    """Raised when creating a conversation whose id is already taken."""


class Turn(BaseModel):
    """One message in a conversation."""

    role: TurnRole
    body: str
    sent_at: datetime
    """When the message was sent (e.g. ``received_at`` from the judge)."""
    recorded_at: datetime
    """When this service recorded the turn."""
    turn_number: int | None = None


class Conversation(BaseModel):
    """A conversation between Vera and a merchant (or a merchant's customer)."""

    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    trigger_id: str | None = None
    state: ConversationState = ConversationState.NEW
    turns: list[Turn] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ConversationStore:
    """Thread-safe conversation store.

    Returned :class:`Conversation` objects are snapshots; mutate state only
    through the store's methods.
    """

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock
        self._conversations: dict[str, Conversation] = {}
        self._lock = threading.Lock()

    def create(
        self,
        conversation_id: str,
        *,
        merchant_id: str | None = None,
        customer_id: str | None = None,
        trigger_id: str | None = None,
        state: ConversationState = ConversationState.NEW,
    ) -> Conversation:
        """Create a conversation. Raises :class:`ConversationExistsError` if the id is taken."""
        with self._lock:
            if conversation_id in self._conversations:
                raise ConversationExistsError(conversation_id)
            conversation = self._new(conversation_id, merchant_id, customer_id, trigger_id, state)
            return conversation.model_copy(deep=True)

    def get_or_create(
        self,
        conversation_id: str,
        *,
        merchant_id: str | None = None,
        customer_id: str | None = None,
        trigger_id: str | None = None,
    ) -> tuple[Conversation, bool]:
        """Return ``(conversation, created)``, creating a ``NEW`` conversation if the id is unknown."""
        with self._lock:
            existing = self._conversations.get(conversation_id)
            if existing is not None:
                return existing.model_copy(deep=True), False
            conversation = self._new(conversation_id, merchant_id, customer_id, trigger_id, ConversationState.NEW)
            return conversation.model_copy(deep=True), True

    def get(self, conversation_id: str) -> Conversation | None:
        """Return a snapshot of the conversation, or ``None`` if unknown."""
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            return None if conversation is None else conversation.model_copy(deep=True)

    def exists(self, conversation_id: str) -> bool:
        """True if the conversation is known."""
        with self._lock:
            return conversation_id in self._conversations

    def update(
        self,
        conversation_id: str,
        *,
        merchant_id: str | None = None,
        customer_id: str | None = None,
        trigger_id: str | None = None,
    ) -> Conversation:
        """Set participant/trigger identifiers. Arguments left as ``None`` are unchanged."""
        with self._lock:
            conversation = self._require(conversation_id)
            if merchant_id is not None:
                conversation.merchant_id = merchant_id
            if customer_id is not None:
                conversation.customer_id = customer_id
            if trigger_id is not None:
                conversation.trigger_id = trigger_id
            conversation.updated_at = self._clock()
            return conversation.model_copy(deep=True)

    def append_message(
        self,
        conversation_id: str,
        *,
        role: TurnRole,
        body: str,
        sent_at: datetime,
        turn_number: int | None = None,
    ) -> Conversation:
        """Append a turn at the end of the conversation, preserving arrival order."""
        with self._lock:
            conversation = self._require(conversation_id)
            now = self._clock()
            conversation.turns.append(
                Turn(role=role, body=body, sent_at=sent_at, recorded_at=now, turn_number=turn_number)
            )
            conversation.updated_at = now
            return conversation.model_copy(deep=True)

    def set_state(self, conversation_id: str, state: ConversationState) -> Conversation:
        """Transition the conversation to ``state``."""
        with self._lock:
            conversation = self._require(conversation_id)
            previous = conversation.state
            conversation.state = state
            conversation.updated_at = self._clock()
            snapshot = conversation.model_copy(deep=True)
        if previous is not state:
            logger.info("conversation state changed conversation_id=%s %s -> %s", conversation_id, previous, state)
        return snapshot

    def clear(self) -> None:
        """Remove every conversation."""
        with self._lock:
            self._conversations.clear()
        logger.info("conversation store cleared")

    def __len__(self) -> int:
        with self._lock:
            return len(self._conversations)

    def _new(
        self,
        conversation_id: str,
        merchant_id: str | None,
        customer_id: str | None,
        trigger_id: str | None,
        state: ConversationState,
    ) -> Conversation:
        now = self._clock()
        conversation = Conversation(
            conversation_id=conversation_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
            trigger_id=trigger_id,
            state=state,
            created_at=now,
            updated_at=now,
        )
        self._conversations[conversation_id] = conversation
        logger.info(
            "conversation created conversation_id=%s merchant_id=%s customer_id=%s trigger_id=%s",
            conversation_id, merchant_id, customer_id, trigger_id,
        )
        return conversation

    def _require(self, conversation_id: str) -> Conversation:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation
