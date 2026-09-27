"""Winner selection (Phase 2D): eligible candidates -> one :class:`DecisionPlan`.

    EligibilityResult[] --filter eligible--> score_candidate() --> rank_candidates() --> winner --> DecisionPlan

Only candidates that Phase 2C marked eligible are ranked; an ineligible
candidate can never win, whatever its score. Scoring and ordering are the
Phase 2A primitives, unchanged: no scope, action-type or ``NO_ACTION``
preference is added here. ``NO_ACTION`` competes like any other candidate.

When nothing is eligible the decision is still a plan: a ``NO_ACTION`` for the
context's trigger, with no evidence. Confidence is computed after the winner is
chosen and never influences it.

Pure: no clock, no I/O, no suppression writes, no message text.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from app.engine.actions import ActionType, CTAType, DecisionScope, SEND_AS_BY_SCOPE
from app.engine.archetypes import classify_trigger
from app.engine.candidates.context import CandidateGenerationContext
from app.engine.eligibility import EligibilityReasonCode, EligibilityResult
from app.engine.plans import DecisionCandidate, DecisionPlan, make_plan_id
from app.engine.scoring import MAX_SCORE, candidate_sort_key, rank_candidates, score_candidate

CONFIDENCE_SCORE_WEIGHT = Decimal("0.5")
CONFIDENCE_MARGIN_WEIGHT = Decimal("0.3")
CONFIDENCE_EVIDENCE_WEIGHT = Decimal("0.2")
MARGIN_SATURATION = Decimal("20")
"""A lead of this many score points (out of 100) over the runner-up counts as full separation."""
CONFIDENCE_PLACES = Decimal("0.0001")

NO_CANDIDATES_OBJECTIVE = "no candidates for this trigger"
NO_ELIGIBLE_OBJECTIVE = "no eligible candidates for this trigger"


class UnplannableTriggerError(ValueError):
    """The trigger kind maps to no archetype, so no plan (not even ``NO_ACTION``) can be built."""


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One eligible candidate's position in the ranking (debugging/tests; not an API shape)."""

    rank: int
    """1-based; rank 1 is the winner."""
    decision_id: str
    """The ``plan_id`` this candidate would get (identity only; independent of score and order)."""
    score: float
    tie_break: tuple
    """``candidate_sort_key`` without the score: urgency, evidence, conversation relevance, expiry, trigger id, identity."""
    candidate: DecisionCandidate


def rank_eligible(results: Iterable[EligibilityResult]) -> tuple[RankedCandidate, ...]:
    """Rank the eligible candidates best-first with the Phase 2A ordering; ineligible ones are dropped first."""
    eligible = [result.candidate for result in results if result.eligible]
    return tuple(
        RankedCandidate(
            rank=rank,
            decision_id=_decision_id(candidate),
            score=score_candidate(candidate),
            tie_break=candidate_sort_key(candidate)[1:],
            candidate=candidate,
        )
        for rank, candidate in enumerate(rank_candidates(eligible), start=1)
    )


def decision_confidence(winner_score: float, runner_up_score: float | None, evidence_strength: float) -> float:
    """Deterministic decision certainty in [0, 1] (not a probability of success).

    ``0.5 * score/100 + 0.3 * min(1, (score - runner_up) / 20) + 0.2 * evidence_strength``,
    with full separation when there is no runner-up; rounded half-even to 4 places.
    """
    score = Decimal(repr(float(winner_score)))
    if runner_up_score is None:
        separation = Decimal(1)
    else:
        lead = score - Decimal(repr(float(runner_up_score)))
        if lead < 0:
            raise ValueError("the winner cannot score below the runner-up")
        separation = min(Decimal(1), lead / MARGIN_SATURATION)
    value = (
        CONFIDENCE_SCORE_WEIGHT * score / Decimal(repr(MAX_SCORE))
        + CONFIDENCE_MARGIN_WEIGHT * separation
        + CONFIDENCE_EVIDENCE_WEIGHT * Decimal(repr(float(evidence_strength)))
    )
    return float(value.quantize(CONFIDENCE_PLACES, rounding=ROUND_HALF_EVEN))


def rationale_facts(candidate: DecisionCandidate) -> tuple[str, ...]:
    """The candidate's evidence renderings, most important first (stable), without duplicates."""
    ordered = sorted(candidate.evidence, key=lambda item: -item.importance)
    return tuple(dict.fromkeys(item.formatted for item in ordered))


def plan_from_candidate(candidate: DecisionCandidate, *, priority_score: float, confidence: float) -> DecisionPlan:
    """The plan for a selected candidate: its decision fields verbatim, plus score, confidence and facts."""
    return DecisionPlan(
        trigger_id=candidate.trigger_id,
        archetype=candidate.archetype,
        scope=candidate.scope,
        merchant_id=candidate.merchant_id,
        customer_id=candidate.customer_id,
        objective=candidate.objective,
        action=candidate.action,
        cta_type=candidate.cta_type,
        send_as=candidate.send_as,
        evidence=candidate.evidence,
        selected_offer_id=candidate.selected_offer_id,
        suppression_key=candidate.suppression_key,
        expires_at=candidate.expires_at,
        priority_score=priority_score,
        confidence=confidence,
        rationale_facts=rationale_facts(candidate),
    )


def select_decision(context: CandidateGenerationContext, results: Sequence[EligibilityResult]) -> DecisionPlan:
    """Select the single decision for ``context``'s trigger from its candidates' eligibility results.

    Returns the top-ranked eligible candidate as a plan, or a ``NO_ACTION`` plan
    when there are no candidates or none is eligible.

    Raises:
        TypeError: if a result is not an :class:`EligibilityResult`.
        ValueError: if an eligible candidate belongs to another trigger or merchant.
        UnplannableTriggerError: if nothing is eligible and the trigger kind is unmapped.
    """
    for result in results:
        if not isinstance(result, EligibilityResult):
            raise TypeError(f"expected EligibilityResult, got {type(result).__name__}")
        if result.eligible and (result.candidate.trigger_id, result.candidate.merchant_id) != (
            context.trigger_id,
            context.merchant_id,
        ):
            raise ValueError(f"eligible candidate for {result.candidate.trigger_id} does not belong to {context.trigger_id}")

    ranking = rank_eligible(results)
    if not ranking:
        return _fallback_plan(context, results)
    winner = ranking[0]
    runner_up = ranking[1].score if len(ranking) > 1 else None
    confidence = decision_confidence(winner.score, runner_up, winner.candidate.evidence_strength)
    return plan_from_candidate(winner.candidate, priority_score=winner.score, confidence=confidence)


def _fallback_plan(context: CandidateGenerationContext, results: Sequence[EligibilityResult]) -> DecisionPlan:
    archetype = classify_trigger(context.trigger)
    if archetype is None:
        raise UnplannableTriggerError(f"trigger kind {context.kind!r} ({context.trigger_id}) has no archetype")
    if results:
        objective = NO_ELIGIBLE_OBJECTIVE
        codes = {code for result in results for code in result.reason_codes}
        facts = (
            f"candidates: {len(results)}",
            "eligible: 0",
            *(f"rejected: {code}" for code in EligibilityReasonCode if code in codes),
        )
    else:
        objective, facts = NO_CANDIDATES_OBJECTIVE, ("candidates: 0",)
    scope = DecisionScope.CUSTOMER if context.customer_id is not None else DecisionScope.MERCHANT
    # Forced decision: with no eligible alternative, staying quiet is certain.
    return DecisionPlan(
        trigger_id=context.trigger_id,
        archetype=archetype,
        scope=scope,
        merchant_id=context.merchant_id,
        customer_id=context.customer_id,
        objective=objective,
        action=ActionType.NO_ACTION,
        cta_type=CTAType.NONE,
        send_as=SEND_AS_BY_SCOPE[scope],
        suppression_key=context.suppression_key,
        expires_at=context.expires_at,
        priority_score=0.0,
        confidence=1.0,
        rationale_facts=facts,
    )


def _decision_id(candidate: DecisionCandidate) -> str:
    return make_plan_id(
        merchant_id=candidate.merchant_id,
        customer_id=candidate.customer_id,
        trigger_id=candidate.trigger_id,
        objective=candidate.objective,
        action=candidate.action,
        suppression_key=candidate.suppression_key,
    )


__all__ = [
    "CONFIDENCE_EVIDENCE_WEIGHT",
    "CONFIDENCE_MARGIN_WEIGHT",
    "CONFIDENCE_SCORE_WEIGHT",
    "MARGIN_SATURATION",
    "NO_CANDIDATES_OBJECTIVE",
    "NO_ELIGIBLE_OBJECTIVE",
    "RankedCandidate",
    "UnplannableTriggerError",
    "decision_confidence",
    "plan_from_candidate",
    "rank_eligible",
    "rationale_facts",
    "select_decision",
]
