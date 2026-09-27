"""ACTIVE_INTENT: the merchant's engagement with Vera.

Three levels of intent, read from the trigger and the conversation:

* **Active planning** (``active_planning_intent``, or an open merchant request
  in the conversation): the merchant already said what they want, so advance
  with a draft. Asking a qualifying question is only proposed when no merchant
  message backs the intent.
* **Curiosity** (``curious_ask_due``): a low-stakes question, unless the
  merchant is waiting on an answer from Vera, in which case the open request is
  answered first and no new question is opened.
* **Dormancy** (``dormant_with_vera``): re-open with a fact about the merchant's
  own numbers, or a light question.
"""

from collections.abc import Iterable

from app.engine.actions import ActionType, CTAType
from app.engine.archetypes import TriggerArchetype
from app.engine.candidates.base import (
    IMPORTANCE_CONTEXT,
    IMPORTANCE_CORE,
    IMPORTANCE_SUPPORT,
    ArchetypeGenerator,
    Proposal,
    kind_topic,
    largest_delta,
    matching_offer,
    no_action,
    offer_evidence,
    signal_evidence,
)
from app.engine.candidates.context import CandidateGenerationContext, ConversationTurnView
from app.engine.features import UNANSWERED_TAG, open_merchant_request, tokens

DRAFT_KEYWORDS: tuple[tuple[frozenset[str], ActionType], ...] = (
    (frozenset({"post"}), ActionType.DRAFT_POST),
    (frozenset({"campaign", "promo", "promotion", "banner"}), ActionType.DRAFT_CAMPAIGN),
    (frozenset({"listing", "profile", "description"}), ActionType.DRAFT_LISTING),
    (frozenset({"list", "message", "whatsapp", "reply", "note"}), ActionType.DRAFT_MESSAGE),
)
"""What Vera offered to prepare, by keyword in her offer; first match wins, else ``draft_artifact``."""

ENGAGED_SIGNALS = ("high_engagement", "engaged_in_last_24h", "engaged_in_last_48h", "growing_views_7d")
DORMANT_SIGNALS = ("dormant_with_vera", "no_recent_conversation")
STALE_CONTENT_SIGNALS = ("stale_posts", "no_recent_post")


def _preceding_vera_turn(ctx: CandidateGenerationContext, turn: ConversationTurnView) -> ConversationTurnView | None:
    earlier = ctx.turns[: ctx.turns.index(turn)]
    return next((t for t in reversed(earlier) if t.role == "vera"), None)


def _draft_action_for(offer_text: str | None) -> ActionType:
    words = tokens(offer_text)
    for keywords, action in DRAFT_KEYWORDS:
        if words & keywords:
            return action
    return ActionType.DRAFT_ARTIFACT


def answer_open_request(ctx: CandidateGenerationContext, request: ConversationTurnView) -> Proposal:
    """Deliver what the merchant asked for in their latest, still-unanswered turn."""
    offer_turn = _preceding_vera_turn(ctx, request)
    return Proposal(
        _draft_action_for(offer_turn.body if offer_turn else None),
        "deliver what the merchant asked for in the open conversation",
        CTAType.CONFIRMATION,
        required=(ctx.turn_evidence(request, "merchant asked", IMPORTANCE_CORE),),
        supporting=(ctx.turn_evidence(offer_turn, "Vera offered", IMPORTANCE_SUPPORT) if offer_turn else None,),
        topic=tokens(request.body, offer_turn.body if offer_turn else None),
        assets=int(offer_turn is not None),
        continues_request=True,
    )


def active_planning_intent(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    intent = ctx.payload_evidence("intent_topic", "merchant's plan", IMPORTANCE_CORE)
    said = ctx.payload_evidence("merchant_last_message", "merchant said", IMPORTANCE_CORE)
    request = open_merchant_request(ctx)
    if intent is None:
        if request is not None:
            return [answer_open_request(ctx, request)]
        return [no_action("wait until the merchant's plan is concrete enough to draft", said)]

    topic = kind_topic(ctx, intent.value)
    suggestion_turn = next(
        (t for t in reversed(ctx.turns) if t.role == "vera" and tokens(intent.value) & tokens(t.body)),
        None,
    )
    suggestion = ctx.turn_evidence(suggestion_turn, "Vera suggested", IMPORTANCE_SUPPORT) if suggestion_turn else None
    request_ev = ctx.turn_evidence(request, "merchant asked", IMPORTANCE_SUPPORT) if request else None
    offer = matching_offer(ctx, intent.value)
    offer_ev = offer_evidence(ctx, offer[0]) if offer else None
    merchant_asked = said is not None or request is not None

    proposals = [
        Proposal(
            ActionType.DRAFT_ARTIFACT,
            "draft a starter version of what the merchant is planning",
            CTAType.CONFIRMATION,
            required=(intent,),
            supporting=(said, request_ev, suggestion, offer_ev),
            topic=topic,
            assets=int(suggestion is not None) + int(offer_ev is not None),
            continues_request=merchant_asked,
        )
    ]
    if offer is not None:
        proposals.append(
            Proposal(
                ActionType.DRAFT_CAMPAIGN,
                "build the planned package on the merchant's existing related offer",
                CTAType.YES_NO,
                required=(intent, offer_ev),
                supporting=(said,),
                topic=topic,
                selected_offer_id=offer[1]["id"],
                assets=1,
            )
        )
    if not merchant_asked:
        proposals.append(
            Proposal(
                ActionType.ASK_MERCHANT,
                "clarify the scope of the plan before drafting it",
                CTAType.OPEN_ENDED,
                required=(intent,),
                topic=topic,
            )
        )
    return proposals


def curious_ask_due(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    request = open_merchant_request(ctx)
    if request is not None:
        return [answer_open_request(ctx, request)]

    template = ctx.payload_evidence("ask_template", "question theme", IMPORTANCE_SUPPORT)
    engaged = signal_evidence(ctx, ENGAGED_SIGNALS, "merchant signal")
    stale = signal_evidence(ctx, STALE_CONTENT_SIGNALS, "content gap")
    topic = kind_topic(ctx, template.value if template else None)
    proposals = [
        Proposal(
            ActionType.ASK_MERCHANT,
            "learn what customers are asking for this week so it can become content",
            CTAType.OPEN_ENDED,
            supporting=(template, *engaged),
            topic=topic,
        )
    ]
    if any(stale):
        proposals.append(
            Proposal(
                ActionType.DRAFT_POST,
                "refresh the merchant's stale listing content",
                CTAType.YES_NO,
                required=(next(e for e in stale if e is not None),),
                supporting=engaged,
                topic=topic,
            )
        )
    return proposals


def dormant_with_vera(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    request = open_merchant_request(ctx)
    if request is not None:
        return [answer_open_request(ctx, request)]

    days = ctx.payload_evidence("days_since_last_merchant_message", "days since the merchant last replied", IMPORTANCE_SUPPORT)
    last_topic = ctx.payload_evidence("last_topic", "last topic", IMPORTANCE_CONTEXT)
    dormant = signal_evidence(ctx, DORMANT_SIGNALS, "merchant signal")
    unanswered_turn = next((t for t in reversed(ctx.turns) if t.role == "vera" and t.engagement == UNANSWERED_TAG), None)
    unanswered = ctx.turn_evidence(unanswered_turn, "unanswered Vera message", IMPORTANCE_CONTEXT) if unanswered_turn else None
    moves = [m for m in (largest_delta(ctx, sign=1), largest_delta(ctx, sign=-1)) if m is not None]
    topic = kind_topic(ctx, last_topic.value if last_topic else None)

    proposals = [
        Proposal(
            ActionType.ASK_MERCHANT,
            "re-open the conversation with a low-effort question",
            CTAType.OPEN_ENDED,
            supporting=(days, last_topic, *dormant, unanswered),
            topic=topic,
        )
    ]
    if moves:
        metric, _ = min(moves, key=lambda m: (-abs(m[1]), m[0]))
        move = ctx.merchant_evidence(f"performance.delta_7d.{metric}", f"{metric} (7d)", IMPORTANCE_CORE, style="change")
        proposals.append(
            Proposal(
                ActionType.SEND_INSIGHT,
                "re-open the conversation with a fresh fact about the merchant's own numbers",
                CTAType.YES_NO,
                required=(move,),
                supporting=(days, *dormant, unanswered),
                topic=topic,
            )
        )
    return proposals


GENERATOR = ArchetypeGenerator(
    TriggerArchetype.ACTIVE_INTENT,
    {
        "active_planning_intent": active_planning_intent,
        "curious_ask_due": curious_ask_due,
        "dormant_with_vera": dormant_with_vera,
    },
)
