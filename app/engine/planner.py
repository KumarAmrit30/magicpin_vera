"""Tick planning (Phase 2E): stored contexts -> decision engine -> emitted actions.

    available_triggers (sorted, de-duplicated)
        -> load contexts (ContextStore)         -> CandidateGenerationContext
        -> generate_candidates      (Phase 2B)
        -> evaluate_candidates      (Phase 2C, read-only suppression)
        -> select_decision          (Phase 2D)  -> one DecisionPlan per trigger
        -> drop NO_ACTION
        -> order by candidate_sort_key of each plan's winning candidate (Phase 2A)
        -> one per suppression key, first MAX_ACTIONS_PER_TICK
        -> TickAction (plan fields verbatim; body/template from compose, Phase 3)
        -> create conversations -> commit suppression -> response

The planner orchestrates only: it never scores, generates, judges eligibility
or re-ranks. A plan is an internal decision; only an emitted action creates a
conversation or a suppression record.

Every emitted action opens a new conversation with a fresh id; a tick never
continues an existing conversation (challenge-testing-brief.md §2.2:
continuation happens via ``/v1/reply``). No conversation is passed to Phase 2C.
Several actions may address one merchant in a tick; the FAQ limit of one
action per ``(merchant_id, conversation_id)`` pair holds because ids are unique.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import ValidationError

from app.engine.candidates import CandidateGenerationContext, generate_candidates
from app.engine.composer import WIRE_CTA, compose
from app.engine.eligibility import EligibilityReasonCode, EligibilityResult, evaluate_candidates
from app.engine.plans import DecisionCandidate, DecisionPlan
from app.engine.scoring import candidate_sort_key
from app.engine.selection import UnplannableTriggerError, rank_eligible, select_decision
from app.models.enums import ContextScope, TurnRole
from app.models.schemas import TickAction
from app.state.container import StateContainer
from app.state.context_store import ContextStore

logger = logging.getLogger(__name__)

MAX_ACTIONS_PER_TICK = 20
"""challenge-testing-brief.md §5: the judge caps a tick at 20 actions."""

CONVERSATION_ID_PREFIX = "conv_"
CONVERSATION_ID_HEX_CHARS = 20


class TriggerOutcome(StrEnum):
    """What the tick did with one available trigger."""

    EMITTED = "emitted"
    NO_ACTION = "no_action"
    DUPLICATE_SUPPRESSION_KEY = "duplicate_suppression_key"
    """A higher-ranked plan already uses the same suppression key this tick."""
    OVER_CAP = "over_cap"
    UNKNOWN_TRIGGER = "unknown_trigger"
    MISSING_CONTEXT = "missing_context"
    INVALID_CONTEXT = "invalid_context"
    UNPLANNABLE = "unplannable"


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    """The tick's handling of one trigger. ``detail`` holds ids only, never payload values."""

    trigger_id: str
    outcome: TriggerOutcome
    candidates: int = 0
    eligible: int = 0
    reason_codes: tuple[EligibilityReasonCode, ...] = ()
    """Rejection codes across all candidates, in ``EligibilityReasonCode`` order."""
    plan: DecisionPlan | None = None
    conversation_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TickResult:
    """Emitted actions (best-ranked first) and every trigger's decision (by trigger id)."""

    actions: tuple[TickAction, ...]
    decisions: tuple[TriggerDecision, ...]


@dataclass(frozen=True, slots=True)
class _Planned:
    decision: TriggerDecision
    winner: DecisionCandidate | None
    context: CandidateGenerationContext | None = None


def plan_tick(state: StateContainer, *, now: datetime, available_triggers: Iterable[str]) -> TickResult:
    """Plan one tick and commit what it emits.

    Reads the stores, decides every available trigger, assembles at most
    :data:`MAX_ACTIONS_PER_TICK` actions, then creates their conversations and
    records their suppression keys. Nothing is written for triggers that are
    not emitted. Serialized per state container, so concurrent ticks cannot
    emit the same decision twice. Exceptions propagate.
    """
    with state.tick_lock:
        planned = [_plan_trigger(state, trigger_id, now) for trigger_id in sorted(set(available_triggers))]
        verdicts, selected = _assemble(planned)
        conversation_ids, actions = _allocate(state, selected, now)
        _commit(state, actions, now)

    decisions = [
        replace(p.decision, outcome=verdicts[p.decision.trigger_id], conversation_id=conversation_ids.get(p.decision.trigger_id))
        if p.decision.trigger_id in verdicts
        else p.decision
        for p in planned
    ]

    by_outcome = {o: sum(d.outcome is o for d in decisions) for o in TriggerOutcome}
    logger.info(
        "tick planned now=%s triggers=%d actions=%d %s",
        now.isoformat(), len(decisions), len(actions),
        " ".join(f"{o}={n}" for o, n in by_outcome.items() if n and o is not TriggerOutcome.EMITTED),
    )
    return TickResult(actions=tuple(actions), decisions=tuple(decisions))


def load_context(contexts: ContextStore, trigger_id: str, now: datetime) -> CandidateGenerationContext | TriggerDecision:
    """The generation context for a stored trigger, or the reason it cannot be built."""
    trigger = contexts.get(ContextScope.TRIGGER, trigger_id)
    if trigger is None:
        return TriggerDecision(trigger_id, TriggerOutcome.UNKNOWN_TRIGGER)
    merchant_id = trigger.payload.get("merchant_id")
    merchant = contexts.get(ContextScope.MERCHANT, merchant_id) if merchant_id else None
    if merchant is None:
        return TriggerDecision(trigger_id, TriggerOutcome.MISSING_CONTEXT, detail=f"merchant={merchant_id}")
    slug = merchant.payload.get("category_slug")
    category = contexts.get(ContextScope.CATEGORY, slug) if slug else None
    if category is None:
        return TriggerDecision(trigger_id, TriggerOutcome.MISSING_CONTEXT, detail=f"category={slug}")
    customer_id = trigger.payload.get("customer_id")
    customer = contexts.get(ContextScope.CUSTOMER, customer_id) if customer_id else None
    if customer_id and customer is None:
        return TriggerDecision(trigger_id, TriggerOutcome.MISSING_CONTEXT, detail=f"customer={customer_id}")
    try:
        return CandidateGenerationContext(
            category=category.payload,
            merchant=merchant.payload,
            trigger=trigger.payload,
            customer=None if customer is None else customer.payload,
            now=now,
        )
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in error.get("loc", ())) or "context" for error in exc.errors()})
        return TriggerDecision(trigger_id, TriggerOutcome.INVALID_CONTEXT, detail=f"invalid: {', '.join(fields)}")


def new_conversation_id(plan: DecisionPlan, now: datetime, is_taken: Callable[[str], bool]) -> str:
    """A fresh id for the conversation an emitted plan opens.

    Derived from the plan identity and the tick time, so it is reproducible;
    a numeric suffix is appended while ``is_taken`` reports a collision.
    """
    identity = json.dumps([plan.plan_id, now.astimezone(UTC).isoformat()], separators=(",", ":"))
    base = f"{CONVERSATION_ID_PREFIX}{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:CONVERSATION_ID_HEX_CHARS]}"
    candidate, attempt = base, 1
    while is_taken(candidate):
        attempt += 1
        candidate = f"{base}_{attempt}"
    return candidate


def tick_action(plan: DecisionPlan, conversation_id: str, context: CandidateGenerationContext) -> TickAction:
    """The wire action for an emitted plan: plan fields verbatim, message text from the Phase 3 composer."""
    if plan.is_no_action:
        raise ValueError(f"no_action plan {plan.plan_id} cannot be emitted")
    message = compose(plan, context)
    return TickAction(
        conversation_id=conversation_id,
        merchant_id=plan.merchant_id,
        customer_id=plan.customer_id,
        send_as=message.send_as,
        trigger_id=plan.trigger_id,
        template_name=message.template_name,
        template_params=list(message.template_params),
        body=message.body,
        cta=message.cta,
        suppression_key=plan.suppression_key,
        rationale=(
            f"{plan.action.value} ({plan.scope.value}) for {plan.trigger_id}: {plan.objective}. "
            f"priority={plan.priority_score} confidence={plan.confidence} plan_id={plan.plan_id}"
        ),
    )


def _plan_trigger(state: StateContainer, trigger_id: str, now: datetime) -> _Planned:
    context = load_context(state.context_store, trigger_id, now)
    if isinstance(context, TriggerDecision):
        return _Planned(context, None)
    results = evaluate_candidates(generate_candidates(context), context, state.suppression_store, now=now)
    counts = _counts(results)
    try:
        plan = select_decision(context, results)
    except UnplannableTriggerError:
        return _Planned(TriggerDecision(trigger_id, TriggerOutcome.UNPLANNABLE, **counts, detail=f"kind={context.kind}"), None)
    if plan.is_no_action:
        return _Planned(TriggerDecision(trigger_id, TriggerOutcome.NO_ACTION, **counts, plan=plan), None)
    winner = rank_eligible(results)[0].candidate
    return _Planned(TriggerDecision(trigger_id, TriggerOutcome.EMITTED, **counts, plan=plan), winner, context)


def _counts(results: list[EligibilityResult]) -> dict:
    codes = {code for result in results for code in result.reason_codes}
    return {
        "candidates": len(results),
        "eligible": sum(result.eligible for result in results),
        "reason_codes": tuple(code for code in EligibilityReasonCode if code in codes),
    }


def _assemble(planned: list[_Planned]) -> tuple[dict[str, TriggerOutcome], list[_Planned]]:
    """Walk actionable plans in Phase 2A order; keep the first per suppression key, up to the cap."""
    ranked = sorted((p for p in planned if p.winner is not None), key=lambda p: candidate_sort_key(p.winner))
    verdicts: dict[str, TriggerOutcome] = {}
    selected: list[_Planned] = []
    keys: set[str] = set()
    for item in ranked:
        plan = item.decision.plan
        if plan.suppression_key in keys:
            verdicts[plan.trigger_id] = TriggerOutcome.DUPLICATE_SUPPRESSION_KEY
        elif len(selected) >= MAX_ACTIONS_PER_TICK:
            verdicts[plan.trigger_id] = TriggerOutcome.OVER_CAP
        else:
            verdicts[plan.trigger_id] = TriggerOutcome.EMITTED
            selected.append(item)
            keys.add(plan.suppression_key)
    return verdicts, selected


def _allocate(state: StateContainer, selected: list[_Planned], now: datetime) -> tuple[dict[str, str], list[TickAction]]:
    """Conversation ids and wire actions for the selected plans, in order. Reads the store, writes nothing.

    Ids are unique within the tick, so each ``(merchant_id, conversation_id)``
    pair carries at most one action (challenge-testing-brief.md FAQ).
    """
    allocated: set[str] = set()
    conversation_ids: dict[str, str] = {}
    actions: list[TickAction] = []
    for item in selected:
        plan = item.decision.plan
        conversation_id = new_conversation_id(plan, now, lambda cid: cid in allocated or state.conversation_store.exists(cid))
        allocated.add(conversation_id)
        conversation_ids[plan.trigger_id] = conversation_id
        actions.append(tick_action(plan, conversation_id, item.context))
    return conversation_ids, actions


def _commit(state: StateContainer, actions: list[TickAction], now: datetime) -> None:
    """Persist emitted actions: conversations first, then suppression keys."""
    for action in actions:
        state.conversation_store.create(
            action.conversation_id,
            merchant_id=action.merchant_id,
            customer_id=action.customer_id,
            trigger_id=action.trigger_id,
        )
        state.conversation_store.append_message(action.conversation_id, role=TurnRole.VERA, body=action.body, sent_at=now)
    for action in actions:
        state.suppression_store.suppress(
            action.suppression_key, reason=f"emitted trigger={action.trigger_id} conversation={action.conversation_id}"
        )


__all__ = [
    "CONVERSATION_ID_PREFIX",
    "MAX_ACTIONS_PER_TICK",
    "WIRE_CTA",
    "TickResult",
    "TriggerDecision",
    "TriggerOutcome",
    "load_context",
    "new_conversation_id",
    "plan_tick",
    "tick_action",
]
