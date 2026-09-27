"""Pure candidate scoring and deterministic ranking.

Nothing here reads the clock, global state, the network or FastAPI. The same
candidate always produces the same score and the same sort key.
"""

import math
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from numbers import Real
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

if TYPE_CHECKING:
    from app.engine.plans import DecisionCandidate

UnitInterval = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
"""A normalized value in [0.0, 1.0]. Out-of-range values are rejected, never clamped."""

URGENCY_WEIGHT = 25.0
TIME_PRESSURE_WEIGHT = 15.0
MERCHANT_RELEVANCE_WEIGHT = 20.0
CONVERSATION_RELEVANCE_WEIGHT = 15.0
ACTIONABILITY_WEIGHT = 10.0
EVIDENCE_STRENGTH_WEIGHT = 10.0
ENGAGEMENT_WEIGHT = 5.0

SCORE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("urgency", URGENCY_WEIGHT),
    ("time_pressure", TIME_PRESSURE_WEIGHT),
    ("merchant_relevance", MERCHANT_RELEVANCE_WEIGHT),
    ("conversation_relevance", CONVERSATION_RELEVANCE_WEIGHT),
    ("actionability", ACTIONABILITY_WEIGHT),
    ("evidence_strength", EVIDENCE_STRENGTH_WEIGHT),
    ("engagement_potential", ENGAGEMENT_WEIGHT),
)
"""Candidate feature name -> weight. Weights sum to :data:`MAX_SCORE`."""

MAX_SCORE = 100.0

_NO_EXPIRY = datetime.max.replace(tzinfo=UTC)


def validate_score_dimension(value: float, name: str = "value") -> float:
    """Return ``value`` as a float if it is a finite real number in [0.0, 1.0].

    Raises:
        TypeError: if ``value`` is not a real number (booleans are rejected).
        ValueError: if ``value`` is NaN, infinite or outside [0.0, 1.0].
    """
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a real number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be within [0.0, 1.0], got {value!r}")
    return number


def score_candidate(candidate: "DecisionCandidate") -> float:
    """Weighted priority score in [0, 100].

    ``score = urgency*25 + time_pressure*15 + merchant_relevance*20
    + conversation_relevance*15 + actionability*10 + evidence_strength*10
    + engagement_potential*5``

    Each feature is re-validated, then summed in decimal arithmetic on its
    shortest decimal representation, so mathematically equal scores are equal
    floats regardless of how the features combine.
    """
    total = Decimal(0)
    for feature, weight in SCORE_WEIGHTS:
        value = validate_score_dimension(getattr(candidate, feature), feature)
        total += Decimal(repr(value)) * Decimal(repr(weight))
    return float(total)


def candidate_sort_key(candidate: "DecisionCandidate") -> tuple:
    """Key for ``sorted()`` that puts the best candidate first.

    Order: higher score, higher urgency, higher evidence strength, higher
    conversation relevance, earlier expiry (no expiry sorts after any expiry),
    lexical ``trigger_id``. Remaining identity fields make the order total, so
    the result never depends on input order.
    """
    return (
        -score_candidate(candidate),
        -candidate.urgency,
        -candidate.evidence_strength,
        -candidate.conversation_relevance,
        (candidate.expires_at is None, candidate.expires_at or _NO_EXPIRY),
        candidate.trigger_id,
        candidate.action.value,
        candidate.merchant_id,
        candidate.customer_id or "",
        candidate.objective,
        candidate.suppression_key,
        candidate.selected_offer_id or "",
        candidate.cta_type.value,
    )


def rank_candidates(candidates: Iterable["DecisionCandidate"]) -> list["DecisionCandidate"]:
    """Return candidates ordered best-first by :func:`candidate_sort_key`."""
    return sorted(candidates, key=candidate_sort_key)
