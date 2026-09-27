"""Deterministic feature extraction for decision candidates.

Each function computes one of the seven :class:`~app.engine.plans.DecisionCandidate`
features in ``[0, 1]`` from the generation context, the candidate's action, and
its grounded evidence. Weighted scoring stays in :mod:`app.engine.scoring`.

``no_action`` candidates describe the case for restraint: they carry no urgency,
time pressure, conversation pull, or engagement, are always executable, and
compete on how strongly the merchant's state supports staying quiet.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.engine.actions import ActionType, DecisionScope
from app.engine.evidence import Evidence, EvidenceSource

if TYPE_CHECKING:
    from app.engine.candidates.context import CandidateGenerationContext, ConversationTurnView

FEATURE_PRECISION = 4

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_MIN_TOKEN_LENGTH = 3
_STOPWORDS = frozenset(
    {
        "and", "for", "the", "with", "this", "that", "from", "your", "you", "what", "would", "like",
        "look", "have", "has", "are", "was", "want", "any", "all", "our", "per", "due", "true", "false",
        "none", "null", "placeholder", "metric", "topic", "kind", "new", "now", "get", "off",
    }
)  # fmt: skip


def tokens(*texts: Any) -> frozenset[str]:
    """Normalized content words in ``texts`` (lowercase, ``_``/punctuation split, trailing ``s`` dropped)."""
    words: set[str] = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        for word in _TOKEN_SPLIT.split(text.lower()):
            if len(word) < _MIN_TOKEN_LENGTH or word in _STOPWORDS or word.isdigit():
                continue
            words.add(word[:-1] if len(word) > 4 and word.endswith("s") else word)
    return frozenset(words)

TRIGGER_URGENCY_MAX = 5

TIME_PRESSURE_STEPS: tuple[tuple[timedelta, float], ...] = (
    (timedelta(hours=24), 1.0),
    (timedelta(hours=72), 0.8),
    (timedelta(days=7), 0.6),
    (timedelta(days=14), 0.4),
    (timedelta(days=30), 0.2),
)
"""``(time left, pressure)``: the first bound the remaining time fits under wins."""

RELEVANCE_BASE = 0.3
RELEVANCE_PER_FACT = 0.1
"""Merchant relevance grows with each merchant/customer fact supporting the candidate."""

EVIDENCE_PEAK_WEIGHT = 0.7
EVIDENCE_BREADTH_WEIGHT = 0.3
EVIDENCE_FULL_BREADTH = 4
"""Corroborating facts (beyond the strongest) needed for full breadth credit."""

ACTIONABILITY_BASE: dict[ActionType, float] = {
    ActionType.NO_ACTION: 1.0,
    ActionType.ASK_MERCHANT: 0.8,
    ActionType.SEND_ALERT: 0.7,
    ActionType.SEND_INSIGHT: 0.6,
    ActionType.RECOMMEND_OPERATIONAL_FIX: 0.5,
    ActionType.RECOMMEND_RETENTION: 0.5,
    ActionType.SEND_CUSTOMER_REMINDER: 0.5,
    ActionType.SEND_CUSTOMER_WINBACK: 0.5,
    ActionType.SEND_CUSTOMER_FOLLOWUP: 0.5,
    ActionType.DRAFT_MESSAGE: 0.5,
    ActionType.DRAFT_CAMPAIGN: 0.4,
    ActionType.DRAFT_LISTING: 0.4,
    ActionType.DRAFT_POST: 0.4,
    ActionType.DRAFT_ARTIFACT: 0.4,
}
ACTIONABILITY_PER_ASSET = 0.15
"""Each concrete grounded asset (offer, slot list, digest item, ...) makes the action easier to execute."""

ENGAGEMENT_BASE: dict[ActionType, float] = {
    ActionType.NO_ACTION: 0.0,
    ActionType.ASK_MERCHANT: 0.6,
    ActionType.DRAFT_ARTIFACT: 0.6,
    ActionType.DRAFT_CAMPAIGN: 0.5,
    ActionType.DRAFT_LISTING: 0.5,
    ActionType.DRAFT_POST: 0.5,
    ActionType.DRAFT_MESSAGE: 0.5,
    ActionType.RECOMMEND_OPERATIONAL_FIX: 0.5,
    ActionType.RECOMMEND_RETENTION: 0.5,
    ActionType.SEND_ALERT: 0.5,
    ActionType.SEND_INSIGHT: 0.4,
    ActionType.SEND_CUSTOMER_REMINDER: 0.5,
    ActionType.SEND_CUSTOMER_FOLLOWUP: 0.5,
    ActionType.SEND_CUSTOMER_WINBACK: 0.4,
}
MERCHANT_ENGAGEMENT_SHIFT = 0.2
CUSTOMER_STATE_SHIFT: dict[str, float] = {"active": 0.1, "new": 0.1, "lapsed_soft": 0.0, "lapsed_hard": -0.1, "churned": -0.2}

ENGAGED_TAGS = frozenset({"merchant_replied", "intent_action", "intent_question", "intent_planning"})
INTENT_TAGS = frozenset({"intent_action", "intent_question", "intent_planning"})
UNANSWERED_TAG = "merchant_no_reply"
ENGAGED_SIGNALS = frozenset({"engaged_in_last_24h", "engaged_in_last_48h", "high_engagement", "active_planning"})
UNRESPONSIVE_SIGNAL_PREFIXES = ("dormant_with_vera", "no_recent_conversation")

REPEATING_ACTIONS = frozenset({ActionType.SEND_ALERT, ActionType.SEND_INSIGHT, ActionType.ASK_MERCHANT})
"""Actions that restate or ask rather than move an acknowledged thread forward."""

CONVERSATION_CONTINUES_REQUEST = 1.0
CONVERSATION_TOPICAL_ENGAGED = 0.9
CONVERSATION_TOPICAL = 0.6
CONVERSATION_TOPICAL_REPEAT = 0.3
CONVERSATION_ENGAGED_OFF_TOPIC = 0.3
CONVERSATION_COLD = 0.1


@dataclass(frozen=True, slots=True)
class CandidateFeatures:
    urgency: float
    time_pressure: float
    merchant_relevance: float
    conversation_relevance: float
    actionability: float
    evidence_strength: float
    engagement_potential: float

    def as_fields(self) -> dict[str, float]:
        return asdict(self)


def _unit(value: float) -> float:
    return round(min(1.0, max(0.0, value)), FEATURE_PRECISION)


# --------------------------------------------------------------------------- #
# Conversation state
# --------------------------------------------------------------------------- #


def merchant_engaged(ctx: "CandidateGenerationContext") -> bool:
    """The merchant's latest tagged turn shows engagement, or a signal says so."""
    merchant_turns = [t for t in ctx.turns if t.role == "merchant" and t.engagement is not None]
    if merchant_turns and merchant_turns[-1].engagement in ENGAGED_TAGS:
        return True
    return bool(ctx.signal_names & ENGAGED_SIGNALS)


def merchant_unresponsive(ctx: "CandidateGenerationContext") -> bool:
    """Vera's latest turn went unanswered, or a signal marks the merchant as dormant."""
    if ctx.turns and ctx.turns[-1].role == "vera" and ctx.turns[-1].engagement == UNANSWERED_TAG:
        return True
    return any(name.startswith(UNRESPONSIVE_SIGNAL_PREFIXES) for name in ctx.signal_names)


def open_merchant_request(ctx: "CandidateGenerationContext") -> "ConversationTurnView | None":
    """The latest turn when it is a merchant request Vera has not answered yet, else ``None``."""
    tagged = [t for t in ctx.turns if t.engagement is not None]
    if tagged and tagged[-1].role == "merchant" and tagged[-1].engagement in INTENT_TAGS:
        return tagged[-1]
    return None


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #


def urgency(ctx: "CandidateGenerationContext", action: ActionType) -> float:
    """Trigger urgency (1-5) scaled to [0.2, 1]; 0 when absent or for ``no_action``."""
    if action is ActionType.NO_ACTION or ctx.urgency is None:
        return 0.0
    return _unit(ctx.urgency / TRIGGER_URGENCY_MAX)


def time_pressure(ctx: "CandidateGenerationContext", action: ActionType, deadline: datetime | None = None) -> float:
    """Pressure from the nearest of the trigger's expiry and a fact-backed deadline.

    Zero without a deadline, once the deadline has passed, or for ``no_action``.
    """
    if action is ActionType.NO_ACTION:
        return 0.0
    deadlines = [d for d in (ctx.expires_at, deadline) if d is not None]
    if not deadlines:
        return 0.0
    remaining = min(deadlines) - ctx.now
    if remaining <= timedelta(0):
        return 0.0
    for bound, pressure in TIME_PRESSURE_STEPS:
        if remaining <= bound:
            return pressure
    return 0.0


def merchant_relevance(evidence: Sequence[Evidence], adjustment: float = 0.0, *, addresses_trigger_subject: bool = True) -> float:
    """How directly the candidate concerns the situation that raised the trigger.

    Base relevance plus a step per supporting merchant/customer fact, plus a
    handler adjustment. A candidate addressed to a different party than the
    trigger's declared ``scope`` (e.g. a merchant-facing draft for a
    customer-scoped trigger) reaches that situation only at one remove and
    forgoes one fact step.
    """
    facts = sum(1 for e in evidence if e.source in (EvidenceSource.MERCHANT, EvidenceSource.CUSTOMER))
    indirect = 0.0 if addresses_trigger_subject else RELEVANCE_PER_FACT
    return _unit(RELEVANCE_BASE + RELEVANCE_PER_FACT * facts + adjustment - indirect)


def addresses_trigger_subject(ctx: "CandidateGenerationContext", scope: DecisionScope) -> bool:
    """True when ``scope`` is the party named by the trigger's declared ``scope`` (validated by the context)."""
    return ctx.trigger["scope"] == scope.value


def conversation_relevance(
    ctx: "CandidateGenerationContext",
    action: ActionType,
    topic: Iterable[str],
    *,
    continues_request: bool = False,
) -> float:
    """How directly the conversation so far concerns this candidate.

    * explicit continuation of the merchant's open request -> 1.0
    * conversation touches the topic and the merchant engaged -> 0.9 for actions
      that move forward, 0.3 for actions that would restate it
    * conversation touches the topic without engagement -> 0.6
    * off-topic conversation -> 0.3 if the merchant is engaged, else 0.1
    * no conversation, or ``no_action`` -> 0.0
    """
    if action is ActionType.NO_ACTION:
        return 0.0
    if continues_request:
        return CONVERSATION_CONTINUES_REQUEST
    if not ctx.turns:
        return 0.0
    engaged = merchant_engaged(ctx)
    topical = bool(frozenset(topic) & tokens(*(t.body for t in ctx.turns)))
    if topical and engaged:
        return CONVERSATION_TOPICAL_REPEAT if action in REPEATING_ACTIONS else CONVERSATION_TOPICAL_ENGAGED
    if topical:
        return CONVERSATION_TOPICAL
    return CONVERSATION_ENGAGED_OFF_TOPIC if engaged else CONVERSATION_COLD


def actionability(action: ActionType, assets: int = 0) -> float:
    """How concretely the action can be executed with what the context provides."""
    if action is ActionType.NO_ACTION:
        return ACTIONABILITY_BASE[action]
    return _unit(ACTIONABILITY_BASE[action] + ACTIONABILITY_PER_ASSET * assets)


def evidence_strength(evidence: Sequence[Evidence]) -> float:
    """How well-supported the candidate is: its strongest fact, plus breadth of corroboration.

    ``0.7 * max(importance) + 0.3 * min(1, (count - 1) / 4)``: a core trigger fact
    with four corroborating facts approaches 1; the trigger kind alone stays low.
    """
    if not evidence:
        return 0.0
    peak = max(item.importance for item in evidence)
    breadth = min(1.0, (len(evidence) - 1) / EVIDENCE_FULL_BREADTH)
    return _unit(EVIDENCE_PEAK_WEIGHT * peak + EVIDENCE_BREADTH_WEIGHT * breadth)


def engagement_potential(ctx: "CandidateGenerationContext", action: ActionType) -> float:
    """Likelihood of a reply: action shape shifted by the recipient's observed engagement."""
    if action is ActionType.NO_ACTION:
        return 0.0
    base = ENGAGEMENT_BASE[action]
    if action.targets_customer:
        state = (ctx.customer or {}).get("state")
        return _unit(base + CUSTOMER_STATE_SHIFT.get(state, 0.0))
    shift = MERCHANT_ENGAGEMENT_SHIFT * (merchant_engaged(ctx) - merchant_unresponsive(ctx))
    return _unit(base + shift)


def compute_features(
    ctx: "CandidateGenerationContext",
    *,
    action: ActionType,
    evidence: Sequence[Evidence],
    topic: Iterable[str] = (),
    deadline: datetime | None = None,
    relevance_adjustment: float = 0.0,
    assets: int = 0,
    continues_request: bool = False,
    time_pressure_override: float | None = None,
    scope: DecisionScope | None = None,
) -> CandidateFeatures:
    """All seven features for one candidate. ``scope`` is the candidate's decision scope (``None``: not known)."""
    pressure = time_pressure(ctx, action, deadline)
    if time_pressure_override is not None and action is not ActionType.NO_ACTION:
        pressure = time_pressure_override
    direct = scope is None or addresses_trigger_subject(ctx, scope)
    return CandidateFeatures(
        urgency=urgency(ctx, action),
        time_pressure=pressure,
        merchant_relevance=merchant_relevance(evidence, relevance_adjustment, addresses_trigger_subject=direct),
        conversation_relevance=conversation_relevance(ctx, action, topic, continues_request=continues_request),
        actionability=actionability(action, assets),
        evidence_strength=evidence_strength(evidence),
        engagement_potential=engagement_potential(ctx, action),
    )
