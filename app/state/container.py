"""Owner of all per-process application state."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.clock import Clock, utc_now
from app.state.context_store import ContextStore
from app.state.conversation_store import ConversationStore
from app.state.suppression_store import SuppressionStore


@dataclass
class StateContainer:
    """Bundles the stores for one application instance (attached to ``app.state``)."""

    context_store: ContextStore
    conversation_store: ConversationStore
    suppression_store: SuppressionStore
    monotonic: Callable[[], float] = time.monotonic
    started_at: float = field(init=False)
    tick_lock: threading.Lock = field(init=False, repr=False, compare=False, default_factory=threading.Lock)
    """Serializes tick planning so read -> decide -> commit is not interleaved with another tick."""

    def __post_init__(self) -> None:
        self.started_at = self.monotonic()

    @classmethod
    def create(cls, clock: Clock = utc_now, monotonic: Callable[[], float] = time.monotonic) -> "StateContainer":
        """Build a container with fresh, empty stores sharing one clock."""
        return cls(
            context_store=ContextStore(clock),
            conversation_store=ConversationStore(clock),
            suppression_store=SuppressionStore(clock),
            monotonic=monotonic,
        )

    def uptime_seconds(self) -> int:
        """Whole seconds since the container was created."""
        return int(self.monotonic() - self.started_at)
