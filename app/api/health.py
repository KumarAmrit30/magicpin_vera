"""``GET /v1/healthz`` and ``GET /v1/metadata``."""

from fastapi import APIRouter

from app.api.deps import SettingsDep, StateDep
from app.config import APPROACH, BOT_NAME, DESCRIPTION, ENGINE, MODEL
from app.models.schemas import ContextCounts, HealthResponse, MetadataResponse

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
def healthz(state: StateDep) -> HealthResponse:
    """Liveness probe with uptime and per-scope context counts."""
    counts = state.context_store.counts()
    return HealthResponse(
        uptime_seconds=state.uptime_seconds(),
        contexts_loaded=ContextCounts(**{scope.value: n for scope, n in counts.items()}),
    )


@router.get("/metadata", response_model=MetadataResponse)
def metadata(settings: SettingsDep) -> MetadataResponse:
    """Static bot identity."""
    return MetadataResponse(
        team_name=settings.team_name,
        team_members=list(settings.team_members),
        model=MODEL,
        approach=APPROACH,
        contact_email=settings.contact_email,
        version=settings.version,
        submitted_at=settings.submitted_at,
        name=BOT_NAME,
        engine=ENGINE,
        description=DESCRIPTION,
    )
