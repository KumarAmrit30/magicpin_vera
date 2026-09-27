"""``POST /v1/reply`` — record an inbound merchant/customer message.

Phase 1A handling (no intent classification):

* unknown ``conversation_id`` -> create it (the judge may reply on conversations
  it started itself, see ``judge_simulator.py``);
* append the message as a new turn;
* ``new`` -> ``waiting``; any other state is preserved;
* respond ``end`` if the conversation is already closed, otherwise ``wait``.
"""

import logging

from fastapi import APIRouter

from app.api.deps import StateDep
from app.models.enums import ConversationState, TurnRole
from app.models.schemas import EndReply, ReplyRequest, ReplyResponse, WaitReply
from app.state.conversation_store import Conversation, ConversationStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reply"])

PLACEHOLDER_WAIT_SECONDS = 1800
WAIT_RATIONALE = "Reply recorded; decision engine not yet enabled."
CLOSED_RATIONALE = "Conversation already closed; reply recorded and no further messages will be sent."


@router.post("/reply", response_model=ReplyResponse)
def reply(body: ReplyRequest, state: StateDep) -> WaitReply | EndReply:
    """Record the reply and return a deterministic placeholder action."""
    store = state.conversation_store
    conversation = _resolve_conversation(store, body)
    logger.info(
        "reply received conversation_id=%s from_role=%s turn_number=%d message_chars=%d",
        body.conversation_id, body.from_role, body.turn_number, len(body.message),
    )
    conversation = store.append_message(
        body.conversation_id,
        role=TurnRole(body.from_role.value),
        body=body.message,
        sent_at=body.received_at,
        turn_number=body.turn_number,
    )

    if conversation.state.is_terminal:
        return EndReply(rationale=CLOSED_RATIONALE)
    if conversation.state is ConversationState.NEW:
        store.set_state(body.conversation_id, ConversationState.WAITING)
    return WaitReply(wait_seconds=PLACEHOLDER_WAIT_SECONDS, rationale=WAIT_RATIONALE)


def _resolve_conversation(store: ConversationStore, body: ReplyRequest) -> Conversation:
    """Fetch or create the conversation and fill in participant ids it was missing."""
    conversation, created = store.get_or_create(
        body.conversation_id, merchant_id=body.merchant_id, customer_id=body.customer_id
    )
    if created:
        return conversation

    for field in ("merchant_id", "customer_id"):
        stored, incoming = getattr(conversation, field), getattr(body, field)
        if stored and incoming and stored != incoming:
            logger.warning(
                "reply %s does not match conversation conversation_id=%s stored=%s incoming=%s",
                field, body.conversation_id, stored, incoming,
            )

    missing = {
        "merchant_id": body.merchant_id if conversation.merchant_id is None else None,
        "customer_id": body.customer_id if conversation.customer_id is None else None,
    }
    if any(missing.values()):
        conversation = store.update(body.conversation_id, **missing)
    return conversation
