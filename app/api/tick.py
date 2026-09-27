"""``POST /v1/tick`` — periodic wake-up.

Delegates to the Phase 2E planner (:func:`app.engine.planner.plan_tick`), which
decides every available trigger and returns at most 20 actions.
"""

from fastapi import APIRouter

from app.api.deps import StateDep
from app.engine.planner import plan_tick
from app.models.schemas import TickRequest, TickResponse

router = APIRouter(tags=["tick"])


@router.post("/tick", response_model=TickResponse)
def tick(body: TickRequest, state: StateDep) -> TickResponse:
    """Plan the tick and return the emitted actions (possibly none)."""
    result = plan_tick(state, now=body.now, available_triggers=body.available_triggers)
    return TickResponse(actions=list(result.actions))
