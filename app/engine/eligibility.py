"""Candidate eligibility and suppression (Phase 2C).

:func:`evaluate_eligibility` decides whether one :class:`DecisionCandidate` may
still be acted on against the *current* context, suppression state and
evaluation time. It sits between candidate generation (Phase 2B) and ranking
(Phase 2D):

    candidates -> eligibility checks -> suppression checks -> EligibilityResult[]

Eligibility is not ranking: it never scores, orders or picks a winner, and
eligible candidates stay independent (no de-duplication). Evaluation is not
commitment: suppression is only *read* (via :class:`SuppressionReader.peek`);
recording a key after a send belongs to the later commit phase.

``NO_ACTION`` sends nothing, so it is exempt from freshness, state,
conversation and suppression checks; it is still checked for integrity and
grounding. The rules and their sources are listed in
``docs/phase-2c-eligibility.md``.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.engine.actions import ActionType, DecisionScope
from app.engine.archetypes import classify_trigger
from app.engine.candidates.context import CandidateGenerationContext, ConversationTurnView
from app.engine.candidates.operations import RENEWABLE_STATUSES
from app.engine.evidence import EvidenceSource
from app.engine.features import ENGAGED_TAGS
from app.engine.plans import DecisionCandidate
from app.models.enums import ConversationState
from app.state.suppression_store import SuppressionRecord

UNANSWERED_NUDGE_LIMIT = 3
"""challenge-brief.md §12.5: stop "after 3 unanswered nudges"."""

MERCHANT_SUPPRESSION_PREFIX = "suppress:merchant:"


def merchant_suppression_key(merchant_id: str) -> str:
    """Store key that, while active, suppresses every trigger for ``merchant_id``."""
    return f"{MERCHANT_SUPPRESSION_PREFIX}{merchant_id}"


class SuppressionReader(Protocol):
    """Read-only view of suppression state. :class:`SuppressionStore` satisfies it."""

    def peek(self, key: str, now: datetime) -> SuppressionRecord | None:
        """The record for ``key`` if active at ``now``; must not modify state."""
        ...


class EligibilityReasonCode(StrEnum):
    """Why a candidate is ineligible. Values are stable, machine-readable and never user-facing.

    Declaration order is the order in which checks run and reasons are reported.
    """

    STRUCTURE_INVALID = "structure_invalid"
    TRIGGER_MISMATCH = "trigger_mismatch"
    TRIGGER_CHANGED = "trigger_changed"
    MERCHANT_MISMATCH = "merchant_mismatch"
    CUSTOMER_NOT_IN_CONTEXT = "customer_not_in_context"
    CONVERSATION_MISMATCH = "conversation_mismatch"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_NOT_GROUNDED = "evidence_not_grounded"
    TRIGGER_EXPIRED = "trigger_expired"
    OFFER_UNAVAILABLE = "offer_unavailable"
    MERCHANT_STATE_CONFLICT = "merchant_state_conflict"
    CONVERSATION_CLOSED = "conversation_closed"
    NUDGE_LIMIT_REACHED = "nudge_limit_reached"
    SUPPRESSED = "suppressed"
    MERCHANT_SUPPRESSED = "merchant_suppressed"


C = EligibilityReasonCode

REASON_SOURCES: Mapping[EligibilityReasonCode, str] = {
    C.STRUCTURE_INVALID: "impl: Phase 2A DecisionCandidate invariants",
    C.TRIGGER_MISMATCH: "impl: candidate must belong to the trigger being evaluated",
    C.TRIGGER_CHANGED: "challenge-testing-brief.md: newer context versions replace older ones; stale composition scores lower",
    C.MERCHANT_MISMATCH: "impl: candidate must belong to the context merchant",
    C.CUSTOMER_NOT_IN_CONTEXT: "challenge-brief.md §4.4: customer context is required for customer-facing messages",
    C.CONVERSATION_MISMATCH: "impl: conversation must belong to the candidate's merchant/customer",
    C.EVIDENCE_MISSING: "challenge-brief.md §5 constraint 8: don't fabricate",
    C.EVIDENCE_NOT_GROUNDED: "challenge-testing-brief.md: stale composition scores lower, hallucinated context lowest",
    C.TRIGGER_EXPIRED: "engagement-design.md: expires_at -- after which the trigger is stale",
    C.OFFER_UNAVAILABLE: "challenge-brief.md §4.2 offer status; §5 constraint 8: no fake offers",
    C.MERCHANT_STATE_CONFLICT: "impl: Phase 2B trigger premises re-checked against merchant state",
    C.CONVERSATION_CLOSED: "api-call-examples.md 2.6: no further messages on an ended conversation_id",
    C.NUDGE_LIMIT_REACHED: "challenge-brief.md §12.5 (open challenge): exit after 3 unanswered nudges",
    C.SUPPRESSED: "challenge-brief.md §4.3 / engagement-design.md: suppression_key dedups re-sends",
    C.MERCHANT_SUPPRESSED: "api-call-examples.md 4.3: suppress all triggers for a hostile merchant",
}


class EligibilityReason(BaseModel):
    """One machine-readable rejection reason. ``detail`` holds ids/paths, never customer payloads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: EligibilityReasonCode
    detail: str = ""
    source: str = ""

    @model_validator(mode="before")
    @classmethod
    def _default_source(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and not data.get("source") and "code" in data:
            return {**data, "source": REASON_SOURCES[EligibilityReasonCode(data["code"])]}
        return data


class EligibilityResult(BaseModel):
    """Eligibility verdict for one candidate. ``eligible`` is true exactly when there are no reasons."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: DecisionCandidate
    eligible: bool
    reasons: tuple[EligibilityReason, ...] = Field(default=())

    @model_validator(mode="after")
    def _check_verdict(self) -> Self:
        if self.eligible == bool(self.reasons):
            raise ValueError("eligible must be true exactly when there are no reasons")
        return self

    @property
    def reason_codes(self) -> tuple[EligibilityReasonCode, ...]:
        return tuple(reason.code for reason in self.reasons)


# --------------------------------------------------------------------------- #
# Checks. Each returns a detail string when the candidate fails, else None.
# --------------------------------------------------------------------------- #


def _structure_errors(candidate: DecisionCandidate) -> str | None:
    try:
        DecisionCandidate.model_validate(candidate.model_dump(warnings=False))
    except ValidationError as exc:
        return "; ".join(sorted({".".join(map(str, e["loc"])) or e["msg"] for e in exc.errors()}))
    return None


def _trigger_changes(candidate: DecisionCandidate, ctx: CandidateGenerationContext) -> str | None:
    changed = [
        name
        for name, current in (
            ("archetype", classify_trigger(ctx.trigger)),
            ("suppression_key", ctx.suppression_key),
            ("expires_at", ctx.expires_at),
        )
        if getattr(candidate, name) != current
    ]
    return ",".join(changed) or None


def _customer_problem(candidate: DecisionCandidate, ctx: CandidateGenerationContext) -> str | None:
    if candidate.customer_id is None:
        return None
    if ctx.customer_id is None:
        return f"customer_id={candidate.customer_id}; context has no customer"
    if candidate.customer_id != ctx.customer_id:
        return f"customer_id={candidate.customer_id}; context customer={ctx.customer_id}"
    return None


def _conversation_problem(candidate: DecisionCandidate, ctx: CandidateGenerationContext) -> str | None:
    conversation = ctx.conversation
    if conversation is None:
        return None
    merchant_id = conversation.get("merchant_id")
    customer_id = conversation.get("customer_id")
    if merchant_id is not None and merchant_id != candidate.merchant_id:
        return f"conversation merchant={merchant_id}"
    if customer_id is not None and customer_id != ctx.customer_id:
        return f"conversation customer={customer_id}"
    return None


def _target_conversation(candidate: DecisionCandidate, ctx: CandidateGenerationContext) -> Mapping[str, Any] | None:
    """The context conversation when it is the thread this candidate would be sent into."""
    conversation = ctx.conversation
    if conversation is None or _conversation_problem(candidate, ctx) is not None:
        return None
    recipient_customer = candidate.customer_id if candidate.scope is DecisionScope.CUSTOMER else None
    return conversation if conversation.get("customer_id") == recipient_customer else None


def _ungrounded_fields(candidate: DecisionCandidate, ctx: CandidateGenerationContext) -> str | None:
    bad = [f"{e.source}:{e.field}" for e in candidate.evidence if not ctx.is_grounded(e)]
    return ",".join(dict.fromkeys(bad)) or None


def _expired(ctx: CandidateGenerationContext, now: datetime) -> str | None:
    expires_at = ctx.expires_at
    if expires_at is not None and now > expires_at:
        return f"expires_at={expires_at.isoformat()}; now={now.isoformat()}"
    return None


def _offer_problem(candidate: DecisionCandidate, ctx: CandidateGenerationContext) -> str | None:
    offer_id = candidate.selected_offer_id
    if offer_id is None or offer_id in {offer["id"] for _, offer in ctx.active_offers}:
        return None
    return f"selected_offer_id={offer_id}"


def _listing_verified(ctx: CandidateGenerationContext) -> str | None:
    return "identity.verified=true" if ctx.value(EvidenceSource.MERCHANT, "identity.verified") is True else None


def _renewal_not_applicable(ctx: CandidateGenerationContext) -> str | None:
    status = ctx.value(EvidenceSource.MERCHANT, "subscription.status")
    return f"subscription.status={status}" if status is not None and status not in RENEWABLE_STATUSES else None


def _winback_not_applicable(ctx: CandidateGenerationContext) -> str | None:
    status = ctx.value(EvidenceSource.MERCHANT, "subscription.status")
    return f"subscription.status={status}" if status == "active" else None


MERCHANT_PREMISES: Mapping[str, Callable[[CandidateGenerationContext], str | None]] = {
    "gbp_unverified": _listing_verified,
    "renewal_due": _renewal_not_applicable,
    "winback_eligible": _winback_not_applicable,
}
"""Trigger kinds whose premise can be contradicted by current merchant state (mirrors Phase 2B restraint)."""


def _conversation_closed(conversation: Mapping[str, Any] | None) -> str | None:
    if conversation is None:
        return None
    state = ConversationState(conversation.get("state") or ConversationState.NEW)
    return f"conversation_id={conversation.get('conversation_id')}; state={state}" if state.is_terminal else None


def _unanswered_streak(turns: Iterable[ConversationTurnView], recipient_role: str) -> int:
    streak = 0
    for turn in turns:
        if turn.role == recipient_role or turn.engagement in ENGAGED_TAGS:
            streak = 0
        elif turn.role:
            streak += 1
    return streak


def _nudge_limit(
    candidate: DecisionCandidate, ctx: CandidateGenerationContext, conversation: Mapping[str, Any] | None
) -> str | None:
    if candidate.scope is DecisionScope.CUSTOMER:
        recipient = "customer"
        turns = [t for t in ctx.turns if t.source is EvidenceSource.CONVERSATION] if conversation is not None else []
    else:
        recipient = "merchant"
        turns = [t for t in ctx.turns if t.source is EvidenceSource.MERCHANT or conversation is not None]
    streak = _unanswered_streak(turns, recipient)
    return f"unanswered={streak}; limit={UNANSWERED_NUDGE_LIMIT}" if streak >= UNANSWERED_NUDGE_LIMIT else None


def _suppressed(suppression: SuppressionReader, key: str, now: datetime) -> str | None:
    record = suppression.peek(key, now)
    if record is None:
        return None
    until = record.expires_at.isoformat() if record.expires_at is not None else "none"
    return f"key={key}; expires_at={until}"


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def evaluate_eligibility(
    candidate: DecisionCandidate,
    context: CandidateGenerationContext,
    suppression: SuppressionReader,
    *,
    now: datetime,
) -> EligibilityResult:
    """Evaluate one candidate. Pure: reads its inputs, mutates nothing, returns every failed rule in order.

    ``now`` is the explicit, timezone-aware evaluation time (normally the tick's ``now``).
    """
    if not isinstance(candidate, DecisionCandidate):
        raise TypeError(f"expected DecisionCandidate, got {type(candidate).__name__}")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    structure = _structure_errors(candidate)
    if structure is not None:
        # The invalid candidate would fail EligibilityResult's own field validation; carry it as-is.
        reason = EligibilityReason(code=C.STRUCTURE_INVALID, detail=structure)
        return EligibilityResult.model_construct(candidate=candidate, eligible=False, reasons=(reason,))

    failures: list[tuple[EligibilityReasonCode, str | None]] = []
    if candidate.trigger_id != context.trigger_id:
        failures.append((C.TRIGGER_MISMATCH, f"trigger_id={candidate.trigger_id}; context trigger={context.trigger_id}"))
    else:
        failures.append((C.TRIGGER_CHANGED, _trigger_changes(candidate, context)))
    if candidate.merchant_id != context.merchant_id:
        failures.append((C.MERCHANT_MISMATCH, f"merchant_id={candidate.merchant_id}; context merchant={context.merchant_id}"))
    failures.append((C.CUSTOMER_NOT_IN_CONTEXT, _customer_problem(candidate, context)))
    failures.append((C.CONVERSATION_MISMATCH, _conversation_problem(candidate, context)))

    sendable = candidate.action is not ActionType.NO_ACTION
    if sendable and not candidate.evidence:
        failures.append((C.EVIDENCE_MISSING, "evidence=()"))
    failures.append((C.EVIDENCE_NOT_GROUNDED, _ungrounded_fields(candidate, context)))

    if sendable:
        conversation = _target_conversation(candidate, context)
        premise = MERCHANT_PREMISES.get(context.canonical_kind or "")
        failures += [
            (C.TRIGGER_EXPIRED, _expired(context, now)),
            (C.OFFER_UNAVAILABLE, _offer_problem(candidate, context)),
            (C.MERCHANT_STATE_CONFLICT, premise(context) if premise else None),
            (C.CONVERSATION_CLOSED, _conversation_closed(conversation)),
            (C.NUDGE_LIMIT_REACHED, _nudge_limit(candidate, context, conversation)),
            (C.SUPPRESSED, _suppressed(suppression, candidate.suppression_key, now)),
            (C.MERCHANT_SUPPRESSED, _suppressed(suppression, merchant_suppression_key(candidate.merchant_id), now)),
        ]
    return _result(candidate, failures)


def evaluate_candidates(
    candidates: Sequence[DecisionCandidate],
    context: CandidateGenerationContext,
    suppression: SuppressionReader,
    *,
    now: datetime,
) -> tuple[EligibilityResult, ...]:
    """Evaluate each candidate independently; results keep the input order (no ranking, no de-duplication)."""
    return tuple(evaluate_eligibility(candidate, context, suppression, now=now) for candidate in candidates)


def eligible_candidates(results: Iterable[EligibilityResult]) -> tuple[DecisionCandidate, ...]:
    """The eligible candidates, in their original order."""
    return tuple(result.candidate for result in results if result.eligible)


def _result(
    candidate: DecisionCandidate, failures: Iterable[tuple[EligibilityReasonCode, str | None]]
) -> EligibilityResult:
    reasons: dict[EligibilityReasonCode, EligibilityReason] = {}
    for code, detail in failures:
        if detail is not None and code not in reasons:
            reasons[code] = EligibilityReason(code=code, detail=detail)
    ordered = tuple(reasons[code] for code in EligibilityReasonCode if code in reasons)
    return EligibilityResult(candidate=candidate, eligible=not ordered, reasons=ordered)


__all__ = [
    "MERCHANT_PREMISES",
    "MERCHANT_SUPPRESSION_PREFIX",
    "REASON_SOURCES",
    "UNANSWERED_NUDGE_LIMIT",
    "EligibilityReason",
    "EligibilityReasonCode",
    "EligibilityResult",
    "SuppressionReader",
    "eligible_candidates",
    "evaluate_candidates",
    "evaluate_eligibility",
    "merchant_suppression_key",
]
