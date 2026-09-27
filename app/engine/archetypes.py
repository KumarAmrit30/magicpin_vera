"""Behavioral trigger archetypes.

An archetype describes *how a trigger should be reasoned about*, not what to
send. Many ``trigger.kind`` values map onto one archetype; the mapping itself
is not part of Phase 2A.
"""

from enum import StrEnum


class TriggerArchetype(StrEnum):
    """Behavioral category a trigger belongs to."""

    SAFETY_COMPLIANCE = "safety_compliance"
    """Regulation, recalls, supply/safety alerts: correctness matters more than engagement."""

    ACTIVE_INTENT = "active_intent"
    """The merchant has signalled intent (planning, follow-up on an open thread)."""

    CUSTOMER_TIMING = "customer_timing"
    """A customer-level window opens (recall, refill, lapse, appointment, trial)."""

    PERFORMANCE = "performance"
    """A change in the merchant's own numbers (spike, dip, milestone)."""

    MARKET_OPPORTUNITY = "market_opportunity"
    """External demand signals (festivals, events, seasonality, research, trends)."""

    COMPETITIVE = "competitive"
    """Competitor activity near the merchant."""

    OPERATIONS = "operations"
    """Account and listing housekeeping (renewal, verification, dormancy, cadence)."""
