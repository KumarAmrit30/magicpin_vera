"""``POST /v1/context`` — receive a versioned context push.

Status codes:

* 200 — first version stored, higher version replaced, or same version re-posted
  (idempotent no-op; ``outcome`` says which).
* 409 — stale: a higher version is already stored; nothing is overwritten.
* 400 — malformed envelope or payload (``reason`` names the offending part).
"""

import logging
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError

from app.api.deps import StateDep
from app.models.domain import parse_context_payload, payload_identifier
from app.models.enums import ContextPutOutcome
from app.models.schemas import (
    ContextAcceptedResponse,
    ContextInvalidResponse,
    ContextStaleResponse,
    VersionedContext,
)
from app.state.context_store import ContextRecord

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context"])

CONTEXT_PATH = "/v1/context"
MAX_ERROR_DETAILS_CHARS = 500

# Envelope fields in the order used to pick a single ``reason`` for a 400.
_REASON_BY_FIELD = {
    "scope": "invalid_scope",
    "context_id": "invalid_context_id",
    "version": "invalid_version",
    "delivered_at": "invalid_delivered_at",
    "payload": "invalid_payload",
}


@router.post(
    "/context",
    response_model=ContextAcceptedResponse,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ContextInvalidResponse},
        status.HTTP_409_CONFLICT: {"model": ContextStaleResponse},
    },
)
def push_context(body: VersionedContext, state: StateDep) -> Response | ContextAcceptedResponse:
    """Validate the payload for its scope, then apply the versioning rule."""
    try:
        parse_context_payload(body.scope, body.payload)
    except ValidationError as exc:
        logger.info("context rejected as invalid scope=%s context_id=%s", body.scope, body.context_id)
        invalid = ContextInvalidResponse(reason="invalid_payload", details=summarize_errors(exc.errors(), ("payload",)))
        return _json(status.HTTP_400_BAD_REQUEST, invalid)

    embedded_id = payload_identifier(body.scope, body.payload)
    if embedded_id is not None and embedded_id != body.context_id:
        logger.warning(
            "context_id does not match payload identifier scope=%s context_id=%s payload_id=%s",
            body.scope, body.context_id, embedded_id,
        )

    result = state.context_store.put(body)
    if result.outcome is ContextPutOutcome.STALE:
        return _json(status.HTTP_409_CONFLICT, ContextStaleResponse(current_version=result.record.version))

    return ContextAcceptedResponse(
        ack_id=make_ack_id(result.record),
        stored_at=result.record.stored_at,
        outcome=result.outcome,
    )


def make_ack_id(record: ContextRecord) -> str:
    """Deterministic acknowledgement id for a stored (scope, context_id, version)."""
    return f"ack_{record.scope}_{record.context_id}_v{record.version}"


def summarize_errors(errors: Sequence[Any], loc_prefix: tuple[str, ...] = ()) -> str:
    """Compact ``field: message`` summary of Pydantic errors, without echoing input values."""
    parts = []
    for error in errors:
        loc = ".".join(str(p) for p in (*loc_prefix, *error.get("loc", ())) if p != "body")
        parts.append(f"{loc}: {error.get('msg', 'invalid')}" if loc else str(error.get("msg", "invalid")))
    return "; ".join(parts)[:MAX_ERROR_DETAILS_CHARS]


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
    """Return the contract's 400 shape for ``/v1/context``; FastAPI's default 422 elsewhere."""
    if request.url.path.rstrip("/") != CONTEXT_PATH:
        return await request_validation_exception_handler(request, exc)
    errors = exc.errors()
    logger.info("context rejected as malformed errors=%d", len(errors))
    invalid = ContextInvalidResponse(reason=_reason_for(errors), details=summarize_errors(errors))
    return _json(status.HTTP_400_BAD_REQUEST, invalid)


def _reason_for(errors: Sequence[Any]) -> str:
    fields = {error["loc"][1] for error in errors if len(error.get("loc", ())) > 1 and error["loc"][0] == "body"}
    for field, reason in _REASON_BY_FIELD.items():
        if field in fields:
            return reason
    return "invalid_request"


def _json(status_code: int, model: BaseModel) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=model.model_dump(mode="json"))
