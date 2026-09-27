"""``POST /v1/reply`` — continue a conversation from an inbound merchant/customer message.

Thin wrapper around :func:`app.engine.reply.handle_reply` (Phase 2F): unknown
``conversation_id`` values are created (the judge replies on conversations it
started itself, see ``judge_simulator.py``), the message is appended, and a
deterministic ``send`` / ``wait`` / ``end`` decision is returned.
"""

from fastapi import APIRouter

from app.api.deps import StateDep
from app.engine.reply import handle_reply
from app.models.schemas import EndReply, ReplyRequest, ReplyResponse, SendReply, WaitReply

router = APIRouter(tags=["reply"])


@router.post("/reply", response_model=ReplyResponse)
def reply(body: ReplyRequest, state: StateDep) -> SendReply | WaitReply | EndReply:
    """Record the reply and return the engine's decision."""
    return handle_reply(state, body).response
