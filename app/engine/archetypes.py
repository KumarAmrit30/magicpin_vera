"""Behavioral trigger archetypes and the ``trigger.kind -> archetype`` classifier.

An archetype describes *how a trigger should be reasoned about*, not what to
send. Many ``trigger.kind`` values map onto one archetype.

The mapping is explicit. Every kind comes from the challenge dataset
(``triggers_seed.json`` plus the kinds ``generate_dataset.py`` emits) or is a
documented alias of one. Unknown kinds are *unmapped* (``None``); they are never
guessed into an archetype.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class TriggerArchetype(StrEnum):
    """Behavioral category a trigger belongs to."""

    SAFETY_COMPLIANCE = "safety_compliance"
    """Regulation changes and product/supply safety alerts: correctness before engagement."""

    ACTIVE_INTENT = "active_intent"
    """The merchant's engagement with Vera: explicit planning intent, curiosity cadence, dormancy."""

    CUSTOMER_TIMING = "customer_timing"
    """A customer-level window opens (recall, refill, lapse, appointment, trial, bridal follow-up)."""

    PERFORMANCE = "performance"
    """A change in the merchant's own numbers (dip, spike, seasonal dip, milestone)."""

    MARKET_OPPORTUNITY = "market_opportunity"
    """External demand signals (research, festivals, seasonality, events, CDE)."""

    COMPETITIVE = "competitive"
    """Competitor activity near the merchant."""

    OPERATIONS = "operations"
    """Account and listing housekeeping (renewal, verification, expired-subscription winback, review issues)."""


TRIGGER_KIND_ARCHETYPES: Mapping[str, TriggerArchetype] = {
    # SAFETY_COMPLIANCE
    "regulation_change": TriggerArchetype.SAFETY_COMPLIANCE,
    "supply_alert": TriggerArchetype.SAFETY_COMPLIANCE,
    # ACTIVE_INTENT
    "active_planning_intent": TriggerArchetype.ACTIVE_INTENT,
    "curious_ask_due": TriggerArchetype.ACTIVE_INTENT,
    "dormant_with_vera": TriggerArchetype.ACTIVE_INTENT,
    # CUSTOMER_TIMING
    "recall_due": TriggerArchetype.CUSTOMER_TIMING,
    "appointment_tomorrow": TriggerArchetype.CUSTOMER_TIMING,
    "trial_followup": TriggerArchetype.CUSTOMER_TIMING,
    "chronic_refill_due": TriggerArchetype.CUSTOMER_TIMING,
    "customer_lapsed_soft": TriggerArchetype.CUSTOMER_TIMING,
    "customer_lapsed_hard": TriggerArchetype.CUSTOMER_TIMING,
    "wedding_package_followup": TriggerArchetype.CUSTOMER_TIMING,
    # PERFORMANCE
    "perf_dip": TriggerArchetype.PERFORMANCE,
    "perf_spike": TriggerArchetype.PERFORMANCE,
    "seasonal_perf_dip": TriggerArchetype.PERFORMANCE,
    "milestone_reached": TriggerArchetype.PERFORMANCE,
    # MARKET_OPPORTUNITY
    "research_digest": TriggerArchetype.MARKET_OPPORTUNITY,
    "festival_upcoming": TriggerArchetype.MARKET_OPPORTUNITY,
    "category_seasonal": TriggerArchetype.MARKET_OPPORTUNITY,
    "ipl_match_today": TriggerArchetype.MARKET_OPPORTUNITY,
    "cde_opportunity": TriggerArchetype.MARKET_OPPORTUNITY,
    # COMPETITIVE
    "competitor_opened": TriggerArchetype.COMPETITIVE,
    # OPERATIONS
    "renewal_due": TriggerArchetype.OPERATIONS,
    "gbp_unverified": TriggerArchetype.OPERATIONS,
    "winback_eligible": TriggerArchetype.OPERATIONS,
    "review_theme_emerged": TriggerArchetype.OPERATIONS,
}
"""Every trigger kind that appears in the challenge dataset (seed + generated)."""

TRIGGER_KIND_ALIASES: Mapping[str, str] = {
    "research_digest_release": "research_digest",  # challenge-brief §4.3/Appendix A, engagement-design
    "category_research_digest_release": "research_digest",  # challenge-brief §4.3 trigger list
    "bridal_followup": "wedding_package_followup",  # case-studies.md Case Study 3
}
"""Documented alternative names for dataset kinds; they share the canonical kind's handling."""


def canonical_trigger_kind(kind: str) -> str | None:
    """Return the dataset kind for ``kind`` (resolving documented aliases), or ``None`` if unknown."""
    canonical = TRIGGER_KIND_ALIASES.get(kind, kind)
    return canonical if canonical in TRIGGER_KIND_ARCHETYPES else None


def classify_trigger_kind(kind: str) -> TriggerArchetype | None:
    """Archetype for a trigger kind; ``None`` when the kind is not known."""
    canonical = canonical_trigger_kind(kind)
    return None if canonical is None else TRIGGER_KIND_ARCHETYPES[canonical]


def classify_trigger(trigger: Mapping[str, Any]) -> TriggerArchetype | None:
    """Archetype for a trigger payload (its ``kind``); ``None`` when missing or unknown."""
    kind = trigger.get("kind")
    return classify_trigger_kind(kind) if isinstance(kind, str) else None
