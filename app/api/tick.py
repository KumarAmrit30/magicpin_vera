"""``POST /v1/tick`` — periodic wake-up.

Phase 1A has no decision engine, so a tick never produces actions.
"""

import logging

from fastapi import APIRouter

from app.api.deps import StateDep
from app.models.enums import ContextScope
from app.models.schemas import TickRequest, TickResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tick"])


@router.post("/tick", response_model=TickResponse)
def tick(body: TickRequest, state: StateDep) -> TickResponse:
    """Validate the tick and return an empty action list."""
    known = sum(state.context_store.exists(ContextScope.TRIGGER, trigger_id) for trigger_id in body.available_triggers)
    logger.info(
        "tick received now=%s available_triggers=%d known_triggers=%d actions=0",
        body.now.isoformat(), len(body.available_triggers), known,
    )
    return TickResponse(actions=[])
