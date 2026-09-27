"""Process configuration (environment variables) and logging setup."""

import logging
import os
from dataclasses import dataclass

from app import __version__

BOT_NAME = "Vera"
ENGINE = "deterministic"
MODEL = "none"
DESCRIPTION = "Deterministic merchant growth message engine"
APPROACH = (
    "Phase 1A foundation: versioned context store, conversation state and suppression store. "
    "Decision engine not yet enabled; /v1/tick returns no actions."
)


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Team identity is supplied via ``VERA_*`` environment variables."""

    team_name: str = BOT_NAME
    team_members: tuple[str, ...] = ()
    contact_email: str | None = None
    submitted_at: str | None = None
    version: str = __version__
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        """Read settings from ``VERA_TEAM_NAME``, ``VERA_TEAM_MEMBERS`` (comma-separated),
        ``VERA_CONTACT_EMAIL``, ``VERA_SUBMITTED_AT`` and ``LOG_LEVEL``."""
        members = os.getenv("VERA_TEAM_MEMBERS", "")
        return cls(
            team_name=os.getenv("VERA_TEAM_NAME", BOT_NAME),
            team_members=tuple(m.strip() for m in members.split(",") if m.strip()),
            contact_email=os.getenv("VERA_CONTACT_EMAIL") or None,
            submitted_at=os.getenv("VERA_SUBMITTED_AT") or None,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )


def configure_logging(level: str) -> None:
    """Configure root logging once; later calls only adjust the ``app`` logger level."""
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("app").setLevel(level)
