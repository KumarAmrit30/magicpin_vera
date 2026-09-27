"""External API contract (request/response models) for the five ``/v1`` endpoints.

Shapes follow ``challenge-testing-brief.md`` §2 and ``examples/api-call-examples.md``.
Deviations from the official contract are documented in ``docs/architecture.md``.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, PlainSerializer, StrictInt

from app.models.enums import ContextPutOutcome, ContextScope, CtaType, FromRole, ReplyAction, SendAs


def format_utc_timestamp(value: datetime) -> str:
    """Render a datetime as ISO-8601 UTC with millisecond precision, e.g. ``2026-04-26T10:00:00.123Z``."""
    utc = value.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


UtcTimestamp = Annotated[AwareDatetime, PlainSerializer(format_utc_timestamp, return_type=str, when_used="json")]
"""Timezone-aware datetime serialized in the challenge's ``...123Z`` format."""

NonEmptyStr = Annotated[str, Field(min_length=1)]


# --------------------------------------------------------------------------- #
# POST /v1/context
# --------------------------------------------------------------------------- #


class VersionedContext(BaseModel):
    """A versioned context push. ``payload`` is kept as arbitrary JSON."""

    scope: ContextScope
    context_id: NonEmptyStr
    version: Annotated[StrictInt, Field(gt=0)]
    payload: dict[str, Any]
    delivered_at: AwareDatetime


class ContextAcceptedResponse(BaseModel):
    """200 response: the push was stored, or was an idempotent repeat of the stored version."""

    accepted: Literal[True] = True
    ack_id: str
    stored_at: UtcTimestamp
    outcome: Literal[ContextPutOutcome.CREATED, ContextPutOutcome.REPLACED, ContextPutOutcome.DUPLICATE]


class ContextStaleResponse(BaseModel):
    """409 response: the store already holds a higher version."""

    accepted: Literal[False] = False
    reason: Literal["stale_version"] = "stale_version"
    current_version: int


class ContextInvalidResponse(BaseModel):
    """400 response: the push was malformed."""

    accepted: Literal[False] = False
    reason: str
    details: str


# --------------------------------------------------------------------------- #
# POST /v1/tick
# --------------------------------------------------------------------------- #


class TickRequest(BaseModel):
    """Periodic wake-up from the judge carrying simulated time and active trigger ids."""

    now: AwareDatetime
    available_triggers: list[str] = Field(default_factory=list)


class TickAction(BaseModel):
    """One proactive send, opening a new conversation."""

    conversation_id: NonEmptyStr
    merchant_id: NonEmptyStr
    customer_id: str | None = None
    send_as: SendAs
    trigger_id: NonEmptyStr
    template_name: NonEmptyStr
    template_params: list[str]
    body: NonEmptyStr
    cta: CtaType
    suppression_key: NonEmptyStr
    rationale: NonEmptyStr


class TickResponse(BaseModel):
    """Zero or more proactive sends (the judge caps a tick at 20 actions)."""

    actions: Annotated[list[TickAction], Field(max_length=20)] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# POST /v1/reply
# --------------------------------------------------------------------------- #


class ReplyRequest(BaseModel):
    """An inbound merchant/customer message on an existing or judge-initiated conversation."""

    conversation_id: NonEmptyStr
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: FromRole
    message: str
    received_at: AwareDatetime
    turn_number: Annotated[StrictInt, Field(ge=1)]


class SendReply(BaseModel):
    """Bot sends a follow-up message."""

    action: Literal[ReplyAction.SEND] = ReplyAction.SEND
    body: NonEmptyStr
    cta: CtaType
    rationale: str


class WaitReply(BaseModel):
    """Bot backs off for ``wait_seconds``."""

    action: Literal[ReplyAction.WAIT] = ReplyAction.WAIT
    wait_seconds: Annotated[int, Field(ge=0)]
    rationale: str


class EndReply(BaseModel):
    """Bot closes the conversation."""

    action: Literal[ReplyAction.END] = ReplyAction.END
    rationale: str


ReplyResponse = Annotated[SendReply | WaitReply | EndReply, Field(discriminator="action")]


# --------------------------------------------------------------------------- #
# GET /v1/healthz, GET /v1/metadata
# --------------------------------------------------------------------------- #


class ContextCounts(BaseModel):
    """Number of stored contexts per scope."""

    category: int = 0
    merchant: int = 0
    customer: int = 0
    trigger: int = 0


class HealthResponse(BaseModel):
    """Liveness probe payload."""

    status: Literal["ok"] = "ok"
    uptime_seconds: int
    contexts_loaded: ContextCounts


class MetadataResponse(BaseModel):
    """Bot identity: the official contract fields plus a short engine description."""

    team_name: str
    team_members: list[str]
    model: str
    approach: str
    contact_email: str | None
    version: str
    submitted_at: str | None
    name: str
    engine: str
    description: str
