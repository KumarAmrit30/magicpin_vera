"""In-memory suppression keys (e.g. ``research:dentists:2026-W17``) with optional expiry.

Expiry is evaluated lazily against a caller-supplied ``now`` so the judge's
simulated time can be used instead of wall-clock time.
"""

import logging
import threading
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.clock import Clock, utc_now

logger = logging.getLogger(__name__)


class SuppressionRecord(BaseModel):
    """A suppressed key and why/until when."""

    model_config = ConfigDict(frozen=True)

    key: str
    created_at: datetime
    expires_at: datetime | None = None
    reason: str | None = None

    def is_active(self, now: datetime) -> bool:
        """True if the suppression has not expired at ``now``."""
        return self.expires_at is None or now < self.expires_at


class SuppressionStore:
    """Thread-safe set of suppression keys."""

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock
        self._records: dict[str, SuppressionRecord] = {}
        self._lock = threading.Lock()

    def suppress(self, key: str, *, reason: str | None = None, expires_at: datetime | None = None) -> SuppressionRecord:
        """Suppress ``key``.

        Re-suppressing an active key keeps its original ``created_at`` and
        replaces ``expires_at``/``reason`` with the new values. Re-suppressing
        an expired key starts a fresh record.
        """
        if not key:
            raise ValueError("suppression key must be non-empty")
        now = self._clock()
        with self._lock:
            existing = self._records.get(key)
            created_at = existing.created_at if existing is not None and existing.is_active(now) else now
            record = SuppressionRecord(key=key, created_at=created_at, expires_at=expires_at, reason=reason)
            self._records[key] = record
        logger.info("suppression set key=%s expires_at=%s reason=%s", key, expires_at, reason)
        return record

    def is_suppressed(self, key: str, now: datetime | None = None) -> bool:
        """True if ``key`` is suppressed and not expired at ``now`` (defaults to the store clock)."""
        return self.get(key, now) is not None

    def get(self, key: str, now: datetime | None = None) -> SuppressionRecord | None:
        """Return the active record for ``key``, dropping it if it has expired."""
        at = now if now is not None else self._clock()
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return None
            if not record.is_active(at):
                del self._records[key]
                return None
            return record

    def clear(self, key: str) -> bool:
        """Remove ``key``. Returns True if it was present."""
        with self._lock:
            removed = self._records.pop(key, None) is not None
        if removed:
            logger.info("suppression cleared key=%s", key)
        return removed

    def clear_all(self) -> None:
        """Remove every suppression key."""
        with self._lock:
            self._records.clear()
        logger.info("suppression store cleared")

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)
