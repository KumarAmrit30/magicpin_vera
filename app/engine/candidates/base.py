"""Generator interface and the shared proposal -> candidate step.

A generator owns one archetype. Inside it, each trigger kind has a small
handler that reads the context and returns :class:`Proposal` objects. A
proposal names an action and the evidence it rests on; :func:`realize` turns it
into a :class:`DecisionCandidate` only when every required fact is present,
then derives scope, ``send_as``, and all seven features.
"""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol, runtime_checkable

from app.engine.actions import SEND_AS_BY_SCOPE, ActionType, CTAType, DecisionScope
from app.engine.archetypes import TriggerArchetype
from app.engine.candidates.context import CandidateGenerationContext
from app.engine.evidence import Evidence, EvidenceSource
from app.engine.features import compute_features, tokens
from app.engine.plans import DecisionCandidate

IMPORTANCE_CORE = 0.9
"""The fact the candidate exists for (the alert, the metric move, the customer's due date)."""
IMPORTANCE_SUPPORT = 0.6
"""Facts that shape the action (an active offer, available slots, a deadline)."""
IMPORTANCE_CONTEXT = 0.4
"""Background that makes the candidate fit this merchant (signals, aggregates, history)."""
IMPORTANCE_KIND = 0.2
"""The trigger kind itself: every candidate cites it, but on its own it is weak evidence."""


@runtime_checkable
class CandidateGenerator(Protocol):
    """Produces grounded candidates for one archetype."""

    archetype: TriggerArchetype

    def generate(self, context: CandidateGenerationContext) -> list[DecisionCandidate]: ...


@dataclass(frozen=True)
class Proposal:
    """A candidate before features: an action plus the facts it depends on.

    ``required`` entries are ``None`` when the fact is missing, which drops the
    proposal. ``supporting`` entries that are ``None`` are skipped.
    """

    action: ActionType
    objective: str
    cta_type: CTAType
    required: tuple[Evidence | None, ...] = ()
    supporting: tuple[Evidence | None, ...] = ()
    topic: frozenset[str] = field(default_factory=frozenset)
    selected_offer_id: str | None = None
    assets: int = 0
    relevance_adjustment: float = 0.0
    deadline: datetime | None = None
    time_pressure_override: float | None = None
    continues_request: bool = False


Handler = Callable[[CandidateGenerationContext], Iterable[Proposal]]


def _evidence_order(evidence: Evidence) -> tuple:
    return (-evidence.importance, evidence.source.value, evidence.field)


def _generation_order(candidate: DecisionCandidate) -> tuple:
    return (
        candidate.action.value,
        candidate.objective,
        candidate.selected_offer_id or "",
        candidate.cta_type.value,
        candidate.customer_id or "",
    )


def realize(ctx: CandidateGenerationContext, archetype: TriggerArchetype, proposal: Proposal) -> DecisionCandidate | None:
    """Build the candidate for ``proposal``, or ``None`` when a required fact is missing."""
    if any(item is None for item in proposal.required):
        return None
    action = proposal.action
    if action.targets_customer and ctx.customer is None:
        return None

    collected: dict[tuple[EvidenceSource, str], Evidence] = {}
    kind = ctx.evidence(EvidenceSource.TRIGGER, "kind", "trigger kind", IMPORTANCE_KIND)
    for item in (kind, *proposal.required, *proposal.supporting):
        if item is not None:
            collected.setdefault((item.source, item.field), item)
    evidence = tuple(sorted(collected.values(), key=_evidence_order))

    customer_scoped = action.targets_customer or (action is ActionType.NO_ACTION and ctx.customer is not None)
    scope = DecisionScope.CUSTOMER if customer_scoped else DecisionScope.MERCHANT
    features = compute_features(
        ctx,
        action=action,
        evidence=evidence,
        topic=proposal.topic,
        deadline=proposal.deadline,
        relevance_adjustment=proposal.relevance_adjustment,
        assets=proposal.assets,
        continues_request=proposal.continues_request,
        time_pressure_override=proposal.time_pressure_override,
    )
    return DecisionCandidate(
        trigger_id=ctx.trigger_id,
        archetype=archetype,
        scope=scope,
        merchant_id=ctx.merchant_id,
        customer_id=ctx.customer_id,
        objective=proposal.objective,
        action=action,
        cta_type=proposal.cta_type,
        send_as=SEND_AS_BY_SCOPE[scope],
        evidence=evidence,
        selected_offer_id=proposal.selected_offer_id,
        suppression_key=ctx.suppression_key,
        expires_at=ctx.expires_at,
        **features.as_fields(),
    )


class ArchetypeGenerator:
    """Dispatches a context to the handler for its trigger kind within one archetype."""

    def __init__(self, archetype: TriggerArchetype, handlers: Mapping[str, Handler]) -> None:
        self.archetype = archetype
        self._handlers = dict(handlers)

    @property
    def kinds(self) -> frozenset[str]:
        """Dataset trigger kinds this generator handles."""
        return frozenset(self._handlers)

    def generate(self, context: CandidateGenerationContext) -> list[DecisionCandidate]:
        handler = self._handlers.get(context.canonical_kind or "")
        if handler is None:
            return []
        candidates = (realize(context, self.archetype, proposal) for proposal in handler(context))
        return sorted((c for c in candidates if c is not None), key=_generation_order)


# --------------------------------------------------------------------------- #
# Shared proposal helpers
# --------------------------------------------------------------------------- #


def no_action(objective: str, *evidence: Evidence | None) -> Proposal:
    """Restraint, backed by the facts that justify staying quiet."""
    return Proposal(ActionType.NO_ACTION, objective, CTAType.NONE, supporting=evidence)


def kind_topic(ctx: CandidateGenerationContext, *texts: Any) -> frozenset[str]:
    """Topic words for conversation matching: the trigger kind plus ``texts``."""
    return tokens(ctx.canonical_kind, *texts)


def offer_evidence(ctx: CandidateGenerationContext, index: int, importance: float = IMPORTANCE_SUPPORT) -> Evidence | None:
    return ctx.merchant_evidence(f"offers.{index}.title", "active offer", importance)


def matching_offer(ctx: CandidateGenerationContext, *phrases: Any) -> tuple[int, Mapping[str, Any]] | None:
    """The active merchant offer sharing the most words with ``phrases`` (ties: lowest offer id)."""
    wanted = tokens(*phrases)
    scored = [
        (len(wanted & tokens(offer["title"])), offer["id"], index, offer)
        for index, offer in ctx.active_offers
        if wanted & tokens(offer["title"])
    ]
    if not scored:
        return None
    _, _, index, offer = min(scored, key=lambda item: (-item[0], item[1]))
    return index, offer


def signal_evidence(ctx: CandidateGenerationContext, names: Iterable[str], label: str) -> tuple[Evidence | None, ...]:
    """Evidence for each named merchant signal that is present, in ``names`` order."""
    found = []
    for name in names:
        index = ctx.signal_index(name)
        if index is not None:
            found.append(ctx.merchant_evidence(f"signals.{index}", label, IMPORTANCE_CONTEXT))
    return tuple(found)


def digest_index(ctx: CandidateGenerationContext, item_id: Any) -> int | None:
    return ctx.find_index(EvidenceSource.CATEGORY, "digest", "id", item_id)


def digest_indices_mentioning(ctx: CandidateGenerationContext, *terms: str) -> list[int]:
    """Category digest items whose title or summary contains one of ``terms`` (as a word)."""
    wanted = tokens(*terms)
    return [
        index
        for index, item in enumerate(ctx.category.get("digest") or [])
        if isinstance(item, Mapping) and wanted & tokens(item.get("title"), item.get("summary"))
    ]


def parse_when(value: Any) -> datetime | None:
    """An aware datetime from an ISO date or datetime string (dates are midnight UTC)."""
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
)}  # fmt: skip
_MONTH_RANGE = re.compile(r"^\s*([A-Za-z]{3})\s*(?:-\s*([A-Za-z]{3}))?\s*$")


def month_in_range(month_range: Any, month: int) -> bool:
    """Whether ``month`` (1-12) falls in a range like ``Apr-Jun``, ``Nov-Feb`` or ``Jan``."""
    if not isinstance(month_range, str) or not (match := _MONTH_RANGE.match(month_range)):
        return False
    start = _MONTHS.get(match.group(1).lower())
    end = _MONTHS.get((match.group(2) or match.group(1)).lower())
    if start is None or end is None:
        return False
    return start <= month <= end if start <= end else month >= start or month <= end


def current_seasonal_beat(ctx: CandidateGenerationContext) -> Evidence | None:
    """The category seasonal beat covering ``ctx.now``'s month, if any."""
    for index, beat in enumerate(ctx.category.get("seasonal_beats") or []):
        if isinstance(beat, Mapping) and month_in_range(beat.get("month_range"), ctx.now.month):
            return ctx.category_evidence(f"seasonal_beats.{index}.note", "seasonal pattern", IMPORTANCE_SUPPORT)
    return None


def signals_with_prefix(ctx: CandidateGenerationContext, prefix: str, label: str) -> tuple[Evidence | None, ...]:
    """Evidence for every merchant signal whose name starts with ``prefix`` (sorted by name)."""
    return signal_evidence(ctx, sorted(n for n in ctx.signal_names if n.startswith(prefix)), label)


def top_review_theme(ctx: CandidateGenerationContext, sentiment: str, importance: float = IMPORTANCE_CONTEXT) -> Evidence | None:
    """The merchant's most frequent review theme with ``sentiment`` (ties: theme name)."""
    themes = [
        (-(theme.get("occurrences_30d") or 0), str(theme.get("theme")), index)
        for index, theme in enumerate(ctx.merchant.get("review_themes") or [])
        if isinstance(theme, Mapping) and theme.get("sentiment") == sentiment and theme.get("theme")
    ]
    if not themes:
        return None
    index = min(themes)[2]
    return ctx.merchant_evidence(f"review_themes.{index}.theme", f"{sentiment} review theme", importance)


def largest_delta(ctx: CandidateGenerationContext, *, sign: int) -> tuple[str, float] | None:
    """The 7-day performance delta with the largest magnitude in direction ``sign`` (+1 / -1)."""
    deltas = (ctx.merchant.get("performance") or {}).get("delta_7d") or {}
    moves = [
        (key, value)
        for key, value in deltas.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value * sign > 0
    ]
    if not moves:
        return None
    return min(moves, key=lambda item: (-abs(item[1]), item[0]))
