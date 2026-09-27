"""Enumerations shared by the API contract, domain models and state layer.

Values mirror the official challenge package (challenge-brief.md,
challenge-testing-brief.md, examples/api-call-examples.md).
"""

from enum import StrEnum


class ContextScope(StrEnum):
    """The four context layers the judge pushes via ``POST /v1/context``."""

    CATEGORY = "category"
    MERCHANT = "merchant"
    CUSTOMER = "customer"
    TRIGGER = "trigger"


class ContextPutOutcome(StrEnum):
    """Result of applying a versioned context push to the store."""

    CREATED = "created"
    """No prior context existed for (scope, context_id); version stored."""

    REPLACED = "replaced"
    """Incoming version was higher than the stored one; atomically replaced."""

    DUPLICATE = "duplicate"
    """Incoming version equals the stored one; idempotent no-op."""

    STALE = "stale"
    """Incoming version is lower than the stored one; rejected."""


class ConversationState(StrEnum):
    """Lifecycle state of a Vera conversation."""

    NEW = "new"
    QUALIFYING = "qualifying"
    COMMITTED = "committed"
    WAITING = "waiting"
    COMPLETED = "completed"
    ENDED = "ended"

    @property
    def is_terminal(self) -> bool:
        """True when no further bot messages may be sent on the conversation."""
        return self in (ConversationState.COMPLETED, ConversationState.ENDED)


class TurnRole(StrEnum):
    """Author of a conversation turn."""

    VERA = "vera"
    MERCHANT = "merchant"
    CUSTOMER = "customer"


class FromRole(StrEnum):
    """Who sent an inbound reply to ``POST /v1/reply``."""

    MERCHANT = "merchant"
    CUSTOMER = "customer"


class ReplyAction(StrEnum):
    """The three valid ``action`` values for a ``/v1/reply`` response."""

    SEND = "send"
    WAIT = "wait"
    END = "end"


class SendAs(StrEnum):
    """Identity a proactive message is sent under."""

    VERA = "vera"
    MERCHANT_ON_BEHALF = "merchant_on_behalf"


class CtaType(StrEnum):
    """Call-to-action shapes used across the official examples."""

    OPEN_ENDED = "open_ended"
    BINARY_YES_NO = "binary_yes_no"
    BINARY_CONFIRM_CANCEL = "binary_confirm_cancel"
    MULTI_CHOICE_SLOT = "multi_choice_slot"
    NONE = "none"


class TriggerScope(StrEnum):
    """Whether a trigger targets the merchant or one of the merchant's customers."""

    MERCHANT = "merchant"
    CUSTOMER = "customer"


class TriggerSource(StrEnum):
    """Where a trigger originated."""

    EXTERNAL = "external"
    INTERNAL = "internal"


class CustomerState(StrEnum):
    """Relationship state of a customer with a merchant."""

    NEW = "new"
    ACTIVE = "active"
    LAPSED_SOFT = "lapsed_soft"
    LAPSED_HARD = "lapsed_hard"
    CHURNED = "churned"
