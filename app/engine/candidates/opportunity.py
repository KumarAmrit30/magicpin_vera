"""MARKET_OPPORTUNITY: research, festivals, seasonality, local events, CDE.

Triggers are evidence, not instructions: an IPL match on a weekend night, with
category evidence that weekend matches underperform, argues *against* a
match-night promotion (Case Study 5). Festivals outside the merchant's category
or far in the future admit restraint. Campaigns only ever use offers the
merchant already runs.
"""

from collections.abc import Iterable, Mapping

from app.engine.actions import ActionType, CTAType
from app.engine.archetypes import TriggerArchetype
from app.engine.candidates.base import (
    IMPORTANCE_CONTEXT,
    IMPORTANCE_CORE,
    IMPORTANCE_SUPPORT,
    ArchetypeGenerator,
    Proposal,
    current_seasonal_beat,
    digest_index,
    digest_indices_mentioning,
    kind_topic,
    no_action,
    offer_evidence,
    parse_when,
    signal_evidence,
)
from app.engine.candidates.context import CandidateGenerationContext
from app.engine.evidence import Evidence, EvidenceSource
from app.engine.features import tokens

FESTIVAL_PLANNING_DAYS = 45
"""Beyond this many days out, acting on a festival is premature."""
SEGMENT_MATCH_MIN_WORDS = 2


def _research_item(ctx: CandidateGenerationContext) -> tuple[EvidenceSource, str, bool] | None:
    """``(source, path, linked)`` of the digest item to discuss; ``linked`` when the trigger names it."""
    index = digest_index(ctx, ctx.payload("top_item_id"))
    if index is not None:
        return EvidenceSource.CATEGORY, f"digest.{index}", True
    if isinstance(ctx.payload("top_item"), Mapping):
        return EvidenceSource.TRIGGER, "payload.top_item", True
    for index, item in enumerate(ctx.category.get("digest") or []):
        if isinstance(item, Mapping) and item.get("kind") == "research" and item.get("title"):
            return EvidenceSource.CATEGORY, f"digest.{index}", False
    return None


def _segment_matches(ctx: CandidateGenerationContext, segment: object) -> tuple[Evidence | None, ...]:
    """Merchant signals and customer aggregates that describe the research's patient segment."""
    wanted = tokens(segment)
    if not wanted:
        return ()
    needed = min(SEGMENT_MATCH_MIN_WORDS, len(wanted))
    signals = sorted(n for n in ctx.signal_names if len(wanted & tokens(n)) >= needed)
    aggregates = sorted(
        key for key in (ctx.merchant.get("customer_aggregate") or {}) if len(wanted & tokens(key)) >= needed
    )
    return (
        *signal_evidence(ctx, signals, "matching cohort"),
        *(ctx.merchant_evidence(f"customer_aggregate.{key}", "matching customers", IMPORTANCE_SUPPORT) for key in aggregates),
    )


def research_digest(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    found = _research_item(ctx)
    if found is None:
        return [no_action("there is no research item to share")]
    source, path, linked = found
    title = ctx.evidence(source, f"{path}.title", "research", IMPORTANCE_CORE if linked else IMPORTANCE_SUPPORT)
    if title is None:
        return [no_action("the research item has no citable finding")]

    journal = ctx.evidence(source, f"{path}.source", "source", IMPORTANCE_SUPPORT)
    trial = ctx.evidence(source, f"{path}.trial_n", "trial size", IMPORTANCE_CONTEXT)
    summary = ctx.evidence(source, f"{path}.summary", "finding", IMPORTANCE_SUPPORT)
    actionable = ctx.evidence(source, f"{path}.actionable", "suggested step", IMPORTANCE_SUPPORT)
    segment = ctx.evidence(source, f"{path}.patient_segment", "relevant segment", IMPORTANCE_CONTEXT)
    matches = _segment_matches(ctx, segment.value if segment else None)
    topic = kind_topic(ctx, title.value)
    return [
        Proposal(
            ActionType.SEND_INSIGHT,
            "share a research finding relevant to the merchant's customers",
            CTAType.NONE,
            required=(title,),
            supporting=(journal, trial, summary, segment, *matches),
            topic=topic,
        ),
        Proposal(
            ActionType.DRAFT_ARTIFACT,
            "turn the research finding into material the merchant can use with customers",
            CTAType.YES_NO,
            required=(title, actionable),
            supporting=(summary, segment, *matches),
            topic=topic,
            assets=1,
        ),
    ]


def festival_upcoming(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    festival = ctx.payload_evidence("festival", "festival", IMPORTANCE_CORE)
    if festival is None:
        return [no_action("the festival is not named in the context")]

    date = ctx.payload_evidence("date", "festival date", IMPORTANCE_SUPPORT)
    days = ctx.payload_evidence("days_until", "days until festival", IMPORTANCE_SUPPORT)
    relevance = ctx.payload_evidence("category_relevance", "relevant categories", IMPORTANCE_SUPPORT)
    category = ctx.merchant_evidence("category_slug", "merchant category", IMPORTANCE_CONTEXT)
    if relevance is not None and isinstance(relevance.value, list) and ctx.merchant["category_slug"] not in relevance.value:
        return [no_action("the festival is not relevant to the merchant's category", festival, relevance, category)]

    topic = kind_topic(ctx, festival.value)
    when = parse_when(ctx.payload("date"))
    proposals = [
        Proposal(
            ActionType.ASK_MERCHANT,
            "learn the merchant's plans for the festival",
            CTAType.OPEN_ENDED,
            required=(festival,),
            supporting=(date, days, relevance, category),
            topic=topic,
            deadline=when,
        )
    ]
    proposals += [
        Proposal(
            ActionType.DRAFT_CAMPAIGN,
            "prepare a festival campaign around an offer the merchant already runs",
            CTAType.YES_NO,
            required=(festival, offer_evidence(ctx, index)),
            supporting=(date, days, relevance, category),
            topic=topic,
            selected_offer_id=offer["id"],
            deadline=when,
            assets=1,
        )
        for index, offer in ctx.active_offers
    ]
    days_until = ctx.payload("days_until")
    if isinstance(days_until, int) and not isinstance(days_until, bool) and days_until > FESTIVAL_PLANNING_DAYS:
        proposals.append(no_action("the festival is too far away to act on yet", festival, days))
    return proposals


def category_seasonal(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    trends = ctx.payload_evidence("trends", "demand trends", IMPORTANCE_CORE) or current_seasonal_beat(ctx)
    if trends is None:
        return [no_action("no seasonal demand signal can be cited")]

    season = ctx.payload_evidence("season", "season", IMPORTANCE_SUPPORT)
    shelf = ctx.payload_evidence("shelf_action_recommended", "shelf action recommended", IMPORTANCE_SUPPORT)
    seasonal_items = [
        i for i, item in enumerate(ctx.category.get("digest") or []) if isinstance(item, Mapping) and item.get("kind") == "seasonal"
    ]
    digest = (
        ctx.category_evidence(f"digest.{seasonal_items[0]}.title", "category digest", IMPORTANCE_SUPPORT) if seasonal_items else None
    )
    topic = kind_topic(ctx, season.value if season else None, trends.value if isinstance(trends.value, str) else None)
    proposals = [
        Proposal(
            ActionType.SEND_INSIGHT,
            "tell the merchant which demand is shifting this season",
            CTAType.NONE,
            required=(trends,),
            supporting=(season, digest),
            topic=topic,
        ),
        Proposal(
            ActionType.DRAFT_POST,
            "let customers know the merchant is ready for the season's demand",
            CTAType.YES_NO,
            required=(trends,),
            supporting=(season,),
            topic=topic,
        ),
    ]
    if shelf is not None and shelf.value is True:
        proposals.append(
            Proposal(
                ActionType.RECOMMEND_OPERATIONAL_FIX,
                "restock and re-shelve for the seasonal demand shift",
                CTAType.YES_NO,
                required=(trends, shelf),
                supporting=(season, digest),
                topic=topic,
                assets=1,
            )
        )
    return proposals


def ipl_match_today(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    match = ctx.payload_evidence("match", "match", IMPORTANCE_CORE)
    if match is None:
        return [no_action("the match is not named in the context")]

    city = ctx.payload_evidence("city", "match city", IMPORTANCE_SUPPORT)
    merchant_city = ctx.merchant_evidence("identity.city", "merchant city", IMPORTANCE_SUPPORT)
    if city and merchant_city and str(city.value).casefold() != str(merchant_city.value).casefold():
        return [no_action("the match is not in the merchant's city", match, city, merchant_city)]

    weeknight = ctx.payload_evidence("is_weeknight", "weeknight match", IMPORTANCE_SUPPORT)
    kickoff = ctx.payload_evidence("match_time_iso", "match time", IMPORTANCE_SUPPORT)
    venue = ctx.payload_evidence("venue", "venue", IMPORTANCE_CONTEXT)
    locality = signal_evidence(ctx, ["ipl_eligible_locality"], "merchant signal")
    delivery = ctx.merchant_evidence("customer_aggregate.delivery_orders_30d", "delivery orders (30d)", IMPORTANCE_CONTEXT)
    ipl_items = digest_indices_mentioning(ctx, "ipl")
    pattern = ctx.category_evidence(f"digest.{ipl_items[0]}.title", "category evidence", IMPORTANCE_CORE) if ipl_items else None
    pattern_detail = ctx.category_evidence(f"digest.{ipl_items[0]}.summary", "category detail", IMPORTANCE_SUPPORT) if ipl_items else None
    topic = kind_topic(ctx, match.value)
    when = parse_when(ctx.payload("match_time_iso"))
    offers = ctx.active_offers

    if weeknight is not None and weeknight.value is False and pattern is not None:
        return [
            Proposal(
                ActionType.SEND_INSIGHT,
                "steer the merchant away from a match-night promotion on a weekend match day",
                CTAType.YES_NO,
                required=(match, weeknight, pattern),
                supporting=(kickoff, venue, pattern_detail, *locality),
                topic=topic,
                deadline=when,
            ),
            *(
                Proposal(
                    ActionType.DRAFT_CAMPAIGN,
                    "push an offer the merchant already runs as a delivery-first special instead of a match-night promotion",
                    CTAType.YES_NO,
                    required=(match, weeknight, offer_evidence(ctx, index)),
                    supporting=(pattern, delivery, kickoff),
                    topic=topic,
                    selected_offer_id=offer["id"],
                    deadline=when,
                    assets=1 + int(delivery is not None),
                )
                for index, offer in offers
            ),
        ]

    proposals = [
        Proposal(
            ActionType.SEND_INSIGHT,
            "flag today's match and what it means for the merchant's demand",
            CTAType.NONE,
            required=(match,),
            supporting=(kickoff, venue, weeknight, pattern, *locality),
            topic=topic,
            deadline=when,
        ),
        *(
            Proposal(
                ActionType.DRAFT_CAMPAIGN,
                "capture match-night demand with an offer the merchant already runs",
                CTAType.YES_NO,
                required=(match, offer_evidence(ctx, index)),
                supporting=(kickoff, weeknight, *locality),
                topic=topic,
                selected_offer_id=offer["id"],
                deadline=when,
                assets=1,
            )
            for index, offer in offers
        ),
    ]
    if not offers:
        proposals.append(
            Proposal(
                ActionType.ASK_MERCHANT,
                "ask whether the merchant wants a plan for match night",
                CTAType.OPEN_ENDED,
                required=(match,),
                supporting=(kickoff, weeknight),
                topic=topic,
                deadline=when,
            )
        )
    return proposals


def cde_opportunity(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    index = digest_index(ctx, ctx.payload("digest_item_id"))
    session = ctx.category_evidence(f"digest.{index}.title", "session", IMPORTANCE_CORE) if index is not None else None
    if session is None:
        return [no_action("the professional-development session is not in the category digest")]

    date = ctx.category_evidence(f"digest.{index}.date", "session date", IMPORTANCE_SUPPORT)
    organiser = ctx.category_evidence(f"digest.{index}.source", "organiser", IMPORTANCE_CONTEXT)
    credits = ctx.payload_evidence("credits", "credits", IMPORTANCE_SUPPORT)
    fee = ctx.payload_evidence("fee", "fee", IMPORTANCE_SUPPORT)
    return [
        Proposal(
            ActionType.SEND_INSIGHT,
            "let the merchant know about a relevant professional-development session",
            CTAType.YES_NO,
            required=(session,),
            supporting=(date, organiser, credits, fee),
            topic=kind_topic(ctx, session.value),
            deadline=parse_when(date.value if date else None),
        )
    ]


GENERATOR = ArchetypeGenerator(
    TriggerArchetype.MARKET_OPPORTUNITY,
    {
        "research_digest": research_digest,
        "festival_upcoming": festival_upcoming,
        "category_seasonal": category_seasonal,
        "ipl_match_today": ipl_match_today,
        "cde_opportunity": cde_opportunity,
    },
)
