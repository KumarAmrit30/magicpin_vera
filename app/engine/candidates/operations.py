"""OPERATIONS: account and listing housekeeping.

Subscription renewal and expired-subscription winback (both about the
merchant's own magicpin account), Google Business Profile verification, and
recurring review complaints. When the merchant's state contradicts the trigger
(an "unverified" listing that is verified, a "winback" for an active
subscription) or the renewal is far off, the candidate is ``no_action``.
"""

from collections.abc import Iterable
from datetime import timedelta

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
    no_action,
    signal_evidence,
    top_review_theme,
)
from app.engine.candidates.context import CandidateGenerationContext
from app.engine.evidence import Evidence
from app.engine.features import UNANSWERED_TAG

RENEWAL_NOTICE_DAYS = 30
"""Renewal is raised only inside this window."""
RENEWABLE_STATUSES = frozenset({"active", "trial"})


def _delta_evidence(ctx: CandidateGenerationContext, sign: int, importance: float) -> Evidence | None:
    move = largest_delta(ctx, sign=sign)
    if move is None:
        return None
    key = move[0]
    return ctx.merchant_evidence(f"performance.delta_7d.{key}", f"{key.removesuffix('_pct')} change (7d)", importance, style="change")


def renewal_due(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    days = ctx.payload_evidence("days_remaining", "days until renewal", IMPORTANCE_CORE) or ctx.merchant_evidence(
        "subscription.days_remaining", "days until renewal", IMPORTANCE_SUPPORT
    )
    status = ctx.merchant_evidence("subscription.status", "subscription status", IMPORTANCE_CONTEXT)
    if days is None or not isinstance(days.value, int) or isinstance(days.value, bool):
        return [no_action("renewal timing is not in the context", status)]
    if status is not None and status.value not in RENEWABLE_STATUSES:
        return [no_action("the subscription is not active, so a renewal reminder does not apply", status, days)]
    if days.value > RENEWAL_NOTICE_DAYS:
        return [no_action("renewal is too far away to raise", days, status)]

    plan = ctx.payload_evidence("plan", "plan", IMPORTANCE_SUPPORT) or ctx.merchant_evidence(
        "subscription.plan", "plan", IMPORTANCE_SUPPORT
    )
    amount = ctx.payload_evidence("renewal_amount", "renewal amount", IMPORTANCE_SUPPORT)
    soon = signal_evidence(ctx, ["renewal_due_soon", "trial_ending_soon"], "merchant signal")
    deadline = ctx.now + timedelta(days=days.value)
    topic = kind_topic(ctx, "subscription renew expire", plan.value if plan else None)
    proposals = [
        Proposal(
            ActionType.SEND_ALERT,
            "make sure the merchant renews before the subscription lapses",
            CTAType.YES_NO,
            required=(days,),
            supporting=(plan, amount, status, *soon),
            topic=topic,
            deadline=deadline,
            assets=int(amount is not None),
        )
    ]
    gain = _delta_evidence(ctx, 1, IMPORTANCE_SUPPORT)
    if gain is not None:
        proposals.append(
            Proposal(
                ActionType.SEND_INSIGHT,
                "show the value the subscription is delivering ahead of renewal",
                CTAType.YES_NO,
                required=(days, gain),
                supporting=(plan,),
                topic=topic,
                deadline=deadline,
            )
        )
    return proposals


def winback_eligible(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    status = ctx.merchant_evidence("subscription.status", "subscription status", IMPORTANCE_SUPPORT)
    if status is not None and status.value == "active":
        return [no_action("the subscription is active, so there is nothing to win back", status)]
    since = ctx.payload_evidence("days_since_expiry", "days since expiry", IMPORTANCE_CORE) or ctx.merchant_evidence(
        "subscription.days_since_expiry", "days since expiry", IMPORTANCE_SUPPORT
    )
    if since is None:
        return [no_action("the subscription lapse is not in the context", status)]

    losses = tuple(
        e
        for e in (
            ctx.payload_evidence("perf_dip_pct", "performance change since expiry", IMPORTANCE_SUPPORT, style="change"),
            ctx.payload_evidence("lapsed_customers_added_since_expiry", "customers lapsed since expiry", IMPORTANCE_SUPPORT),
            _delta_evidence(ctx, -1, IMPORTANCE_CONTEXT),
        )
        if e is not None
    )
    signals = signal_evidence(ctx, ["winback_eligible", "perf_dip_post_expiry"], "merchant signal")
    unanswered_turn = next((t for t in reversed(ctx.turns) if t.role == "vera" and t.engagement == UNANSWERED_TAG), None)
    unanswered = ctx.turn_evidence(unanswered_turn, "unanswered Vera message", IMPORTANCE_CONTEXT) if unanswered_turn else None
    topic = kind_topic(ctx, "subscription expired renew")
    proposals = [
        Proposal(
            ActionType.ASK_MERCHANT,
            "understand what would bring the merchant back after the subscription lapsed",
            CTAType.OPEN_ENDED,
            required=(since,),
            supporting=(status, unanswered),
            topic=topic,
        )
    ]
    if losses:
        proposals.append(
            Proposal(
                ActionType.SEND_INSIGHT,
                "show the merchant what has slipped since the subscription lapsed",
                CTAType.YES_NO,
                required=(since, losses[0]),
                supporting=(*losses[1:], status, *signals),
                topic=topic,
            )
        )
    return proposals


def gbp_unverified(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    listed = ctx.merchant_evidence("identity.verified", "listing verified", IMPORTANCE_CORE)
    if listed is not None and listed.value is True:
        return [no_action("the listing is already verified", listed)]
    unverified = ctx.payload_evidence("verified", "listing verified", IMPORTANCE_CORE)
    if unverified is not None and unverified.value is True:
        return [no_action("the trigger reports the listing as verified", unverified)]
    signal = signal_evidence(ctx, ["unverified_gbp"], "merchant signal")
    grounding = next((e for e in (unverified, listed, *signal) if e is not None), None)
    if grounding is None:
        return [no_action("the listing's verification status is not in the context")]

    path = ctx.payload_evidence("verification_path", "verification path", IMPORTANCE_SUPPORT)
    uplift = ctx.payload_evidence("estimated_uplift_pct", "estimated uplift", IMPORTANCE_SUPPORT, style="change")
    topic = kind_topic(ctx, "google profile verify verification")
    return [
        Proposal(
            ActionType.RECOMMEND_OPERATIONAL_FIX,
            "get the merchant's Google Business Profile verified",
            CTAType.YES_NO,
            required=(grounding,),
            supporting=(unverified, listed, *signal, path, uplift),
            topic=topic,
            assets=int(path is not None),
        ),
        Proposal(
            ActionType.ASK_MERCHANT,
            "find out which verification route works for the merchant",
            CTAType.OPEN_ENDED,
            required=(grounding, path),
            topic=topic,
        ),
    ]


def review_theme_emerged(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    theme = ctx.payload_evidence("theme", "review theme", IMPORTANCE_CORE)
    if theme is None:
        theme = top_review_theme(ctx, "neg", IMPORTANCE_SUPPORT)
    if theme is None:
        return [no_action("no review theme is on record")]

    occurrences = ctx.payload_evidence("occurrences_30d", "mentions (30d)", IMPORTANCE_SUPPORT)
    trend = ctx.payload_evidence("trend", "trend", IMPORTANCE_SUPPORT)
    quote = ctx.payload_evidence("common_quote", "customer quote", IMPORTANCE_SUPPORT)
    index = next(
        (i for i, t in enumerate(ctx.merchant.get("review_themes") or []) if isinstance(t, dict) and t.get("theme") == theme.value),
        None,
    )
    sentiment = ctx.merchant_evidence(f"review_themes.{index}.sentiment", "sentiment", IMPORTANCE_SUPPORT) if index is not None else None
    if occurrences is None and index is not None:
        occurrences = ctx.merchant_evidence(f"review_themes.{index}.occurrences_30d", "mentions (30d)", IMPORTANCE_SUPPORT)
    facts = (occurrences, trend, quote, sentiment)
    topic = kind_topic(ctx, theme.value)

    if sentiment is not None and sentiment.value == "pos":
        return [
            Proposal(
                ActionType.SEND_INSIGHT,
                "tell the merchant what customers keep praising",
                CTAType.NONE,
                required=(theme, sentiment),
                supporting=facts,
                topic=topic,
            ),
            Proposal(
                ActionType.DRAFT_POST,
                "showcase what customers keep praising",
                CTAType.YES_NO,
                required=(theme, sentiment),
                supporting=facts,
                topic=topic,
            ),
        ]
    return [
        Proposal(
            ActionType.RECOMMEND_OPERATIONAL_FIX,
            "fix the operational issue customers keep raising in reviews",
            CTAType.YES_NO,
            required=(theme,),
            supporting=facts,
            topic=topic,
        ),
        Proposal(
            ActionType.DRAFT_MESSAGE,
            "draft responses to the reviews raising the issue",
            CTAType.YES_NO,
            required=(theme,),
            supporting=facts,
            topic=topic,
            assets=int(quote is not None),
        ),
    ]


GENERATOR = ArchetypeGenerator(
    TriggerArchetype.OPERATIONS,
    {
        "renewal_due": renewal_due,
        "winback_eligible": winback_eligible,
        "gbp_unverified": gbp_unverified,
        "review_theme_emerged": review_theme_emerged,
    },
)
