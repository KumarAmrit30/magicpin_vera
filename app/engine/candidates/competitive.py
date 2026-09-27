"""COMPETITIVE: competitor activity near the merchant.

Competitor facts come only from the trigger payload (name, distance, their
offer, opening date). Nothing about the competitor is inferred. When the
trigger names no competitor, the only merchant-facing move is to strengthen the
listing on what customers already praise, without mentioning a competitor.
"""

from collections.abc import Iterable

from app.engine.actions import ActionType, CTAType
from app.engine.archetypes import TriggerArchetype
from app.engine.candidates.base import (
    IMPORTANCE_CORE,
    IMPORTANCE_SUPPORT,
    ArchetypeGenerator,
    Proposal,
    kind_topic,
    matching_offer,
    no_action,
    offer_evidence,
    signal_evidence,
    top_review_theme,
)
from app.engine.candidates.context import CandidateGenerationContext

LISTING_GAP_SIGNALS = ("stale_posts", "no_recent_post", "ctr_below_peer_median", "unverified_gbp")


def competitor_opened(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    strength = top_review_theme(ctx, "pos", IMPORTANCE_SUPPORT)
    gaps = signal_evidence(ctx, LISTING_GAP_SIGNALS, "listing gap")
    name = ctx.payload_evidence("competitor_name", "competitor", IMPORTANCE_CORE)
    listing_objective = "differentiate the listing on what customers already praise rather than on price"

    if name is None:
        proposals = [no_action("competitor details are not in the context, so they cannot be discussed")]
        if strength is not None:
            proposals.append(
                Proposal(
                    ActionType.DRAFT_LISTING,
                    listing_objective,
                    CTAType.YES_NO,
                    required=(strength,),
                    supporting=gaps,
                    topic=kind_topic(ctx),
                )
            )
        return proposals

    distance = ctx.payload_evidence("distance_km", "distance (km)", IMPORTANCE_SUPPORT)
    their_offer = ctx.payload_evidence("their_offer", "their offer", IMPORTANCE_SUPPORT)
    opened = ctx.payload_evidence("opened_date", "opened", IMPORTANCE_SUPPORT)
    topic = kind_topic(ctx, name.value, their_offer.value if their_offer else None)
    facts = (distance, their_offer, opened)
    proposals = [
        Proposal(
            ActionType.SEND_INSIGHT,
            "tell the merchant a competitor opened nearby, using only the facts on record",
            CTAType.NONE,
            required=(name,),
            supporting=facts,
            topic=topic,
        ),
        Proposal(
            ActionType.ASK_MERCHANT,
            "ask how the merchant wants to position against the new competitor",
            CTAType.OPEN_ENDED,
            required=(name,),
            supporting=facts,
            topic=topic,
        ),
        Proposal(
            ActionType.DRAFT_LISTING,
            listing_objective,
            CTAType.YES_NO,
            required=(name, strength),
            supporting=(distance, *gaps),
            topic=topic,
            assets=1,
        ),
    ]
    comparable = matching_offer(ctx, their_offer.value) if their_offer else None
    if comparable is not None:
        index, offer = comparable
        proposals.append(
            Proposal(
                ActionType.DRAFT_CAMPAIGN,
                "answer the competitor's offer with the merchant's own comparable offer",
                CTAType.YES_NO,
                required=(name, their_offer, offer_evidence(ctx, index)),
                supporting=(distance, strength),
                topic=topic,
                selected_offer_id=offer["id"],
                assets=1,
            )
        )
    return proposals


GENERATOR = ArchetypeGenerator(TriggerArchetype.COMPETITIVE, {"competitor_opened": competitor_opened})
