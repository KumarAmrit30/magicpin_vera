"""Versioned, in-memory store for the four context scopes.

Versioning rule (challenge-testing-brief.md §2.1), keyed by ``(scope, context_id)``:

* nothing stored          -> store it                  (``CREATED``)
* incoming > stored       -> replace atomically        (``REPLACED``)
* incoming == stored      -> idempotent no-op          (``DUPLICATE``)
* incoming < stored       -> reject, keep newer state  (``STALE``)
"""

import copy
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.clock import Clock, utc_now
from app.models.enums import ContextPutOutcome, ContextScope
from app.models.schemas import VersionedContext

logger = logging.getLogger(__name__)


class ContextRecord(BaseModel):
    """A stored context version. Treat ``payload`` as read-only."""

    model_config = ConfigDict(frozen=True)

    scope: ContextScope
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: datetime
    stored_at: datetime


@dataclass(frozen=True, slots=True)
class PutResult:
    """Outcome of :meth:`ContextStore.put`.

    ``record`` is always the version held by the store *after* the call — the new
    record for ``CREATED``/``REPLACED``, the existing one for ``DUPLICATE``/``STALE``.
    """

    outcome: ContextPutOutcome
    record: ContextRecord
    previous_version: int | None

    @property
    def accepted(self) -> bool:
        """True unless the push was stale."""
        return self.outcome is not ContextPutOutcome.STALE


class ContextStore:
    """Thread-safe store of the latest version of every ``(scope, context_id)``."""

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock
        self._records: dict[tuple[ContextScope, str], ContextRecord] = {}
        self._lock = threading.Lock()

    def put(self, context: VersionedContext) -> PutResult:
        """Apply a versioned push according to the versioning rule. Never overwrites a newer version."""
        key = (context.scope, context.context_id)
        with self._lock:
            current = self._records.get(key)
            if current is not None and context.version <= current.version:
                outcome = ContextPutOutcome.DUPLICATE if context.version == current.version else ContextPutOutcome.STALE
                self._log_ignored(outcome, context, current)
                return PutResult(outcome=outcome, record=current, previous_version=current.version)

            record = ContextRecord(
                scope=context.scope,
                context_id=context.context_id,
                version=context.version,
                payload=copy.deepcopy(context.payload),
                delivered_at=context.delivered_at,
                stored_at=self._clock(),
            )
            self._records[key] = record

        outcome = ContextPutOutcome.CREATED if current is None else ContextPutOutcome.REPLACED
        previous_version = None if current is None else current.version
        logger.info(
            "context accepted scope=%s context_id=%s version=%d outcome=%s previous_version=%s",
            context.scope, context.context_id, context.version, outcome, previous_version,
        )
        return PutResult(outcome=outcome, record=record, previous_version=previous_version)

    def get(self, scope: ContextScope, context_id: str) -> ContextRecord | None:
        """Return the stored record, or ``None`` if absent."""
        with self._lock:
            return self._records.get((scope, context_id))

    def get_all(self, scope: ContextScope) -> list[ContextRecord]:
        """Return every record in ``scope``, ordered by ``context_id``."""
        with self._lock:
            records = [record for (s, _), record in self._records.items() if s is scope]
        return sorted(records, key=lambda record: record.context_id)

    def exists(self, scope: ContextScope, context_id: str) -> bool:
        """True if a record is stored for ``(scope, context_id)``."""
        with self._lock:
            return (scope, context_id) in self._records

    def counts(self) -> dict[ContextScope, int]:
        """Number of stored records per scope (every scope present, zero if empty)."""
        counts = dict.fromkeys(ContextScope, 0)
        with self._lock:
            for scope, _ in self._records:
                counts[scope] += 1
        return counts

    def clear(self) -> None:
        """Remove every stored record."""
        with self._lock:
            self._records.clear()
        logger.info("context store cleared")

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    @staticmethod
    def _log_ignored(outcome: ContextPutOutcome, context: VersionedContext, current: ContextRecord) -> None:
        if outcome is ContextPutOutcome.DUPLICATE:
            if context.payload != current.payload:
                logger.warning(
                    "context ignored as duplicate but payload differs scope=%s context_id=%s version=%d",
                    context.scope, context.context_id, context.version,
                )
            else:
                logger.info(
                    "context ignored as duplicate scope=%s context_id=%s version=%d",
                    context.scope, context.context_id, context.version,
                )
        else:
            logger.info(
                "context rejected as stale scope=%s context_id=%s incoming_version=%d current_version=%d",
                context.scope, context.context_id, context.version, current.version,
            )
