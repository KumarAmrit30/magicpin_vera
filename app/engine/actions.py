"""Controlled vocabularies for what a decision does, whom it targets, and how it asks.

``SendAs`` is re-exported from :mod:`app.models.enums` rather than redefined:
the challenge defines one send-as vocabulary (``vera`` for merchant-facing,
``merchant_on_behalf`` for customer-facing) shared by the API and the engine.

``CTAType`` here is the *planning-level* CTA intent. It is distinct from
:class:`app.models.enums.CtaType`, which holds the wire values sent to the
judge (``binary_yes_no``, ``multi_choice_slot``, ...). Phase 3 maps one onto the other.
"""

from enum import StrEnum

from app.models.enums import SendAs


class ActionType(StrEnum):
    """What the decision does. ``NO_ACTION`` is a valid, first-class decision."""

    NO_ACTION = "no_action"

    SEND_INSIGHT = "send_insight"
    SEND_ALERT = "send_alert"

    DRAFT_CAMPAIGN = "draft_campaign"
    DRAFT_LISTING = "draft_listing"
    DRAFT_POST = "draft_post"
    DRAFT_MESSAGE = "draft_message"
    DRAFT_ARTIFACT = "draft_artifact"

    SEND_CUSTOMER_REMINDER = "send_customer_reminder"
    SEND_CUSTOMER_WINBACK = "send_customer_winback"
    SEND_CUSTOMER_FOLLOWUP = "send_customer_followup"

    RECOMMEND_RETENTION = "recommend_retention"
    RECOMMEND_OPERATIONAL_FIX = "recommend_operational_fix"

    ASK_MERCHANT = "ask_merchant"

    @property
    def targets_customer(self) -> bool:
        """True for actions whose recipient is the merchant's customer."""
        return self in CUSTOMER_ACTIONS


CUSTOMER_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.SEND_CUSTOMER_REMINDER,
        ActionType.SEND_CUSTOMER_WINBACK,
        ActionType.SEND_CUSTOMER_FOLLOWUP,
    }
)


class DecisionScope(StrEnum):
    """Who the decided action targets (not which context layer the data came from)."""

    MERCHANT = "merchant"
    CUSTOMER = "customer"


class CTAType(StrEnum):
    """Planning-level shape of the call to action. Wording is produced in Phase 3."""

    NONE = "none"
    YES_NO = "yes_no"
    OPEN_ENDED = "open_ended"
    CONFIRMATION = "confirmation"


SEND_AS_BY_SCOPE: dict[DecisionScope, SendAs] = {
    DecisionScope.MERCHANT: SendAs.VERA,
    DecisionScope.CUSTOMER: SendAs.MERCHANT_ON_BEHALF,
}
"""Send-as identity implied by the decision scope (challenge-brief.md §5)."""

__all__ = [
    "CUSTOMER_ACTIONS",
    "SEND_AS_BY_SCOPE",
    "ActionType",
    "CTAType",
    "DecisionScope",
    "SendAs",
]
