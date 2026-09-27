"""PERFORMANCE: movement in the merchant's own numbers.

* A **dip** is alerted, and paired with a concrete fix only when a listing gap is
  on record. It becomes a campaign only when an active offer exists to promote.
* A **spike** is shared as good news and a way to repeat what drove it; it is
  never framed as a problem.
* An **expected seasonal dip** is reframed, not alarmed; restraint is a real option.
* A **milestone** is shared only when both the current value and the milestone
  are in the context.

The move comes from the trigger payload (``metric`` / ``delta_pct``) or, for
payload-less triggers, from ``merchant.performance.delta_7d``. If the numbers
contradict the trigger (a "dip" with no negative delta) the only candidate is
``no_action``.
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
    current_seasonal_beat,
    kind_topic,
    largest_delta,
    no_action,
    offer_evidence,
    signal_evidence,
    signals_with_prefix,
    top_review_theme,
)
from app.engine.candidates.context import CandidateGenerationContext
from app.engine.evidence import Evidence
from app.engine.features import tokens

LISTING_GAP_SIGNALS = (
    "unverified_gbp",
    "stale_posts",
    "no_recent_post",
    "ctr_below_peer_median",
    "no_active_offers",
    "delivery_not_set_up",
)
GROWTH_SIGNALS = ("growing_views_7d", "above_peer_median_calls", "above_peer_calls", "above_peer_ctr", "stable_growth")
MEMBER_AGGREGATES: tuple[tuple[str, str, str | None], ...] = (
    ("total_active_members", "active members", None),
    ("repeat_customer_pct", "repeat customers", "rate"),
    ("retention_6mo_pct", "6-month retention", "rate"),
    ("retention_3mo_pct", "3-month retention", "rate"),
)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def metric_move(ctx: CandidateGenerationContext, sign: int) -> tuple[Evidence | None, str | None]:
    """The move in direction ``sign`` and its metric name; ``(None, None)`` when absent or contradicted."""
    delta, metric = ctx.payload("delta_pct"), ctx.payload("metric")
    if _is_number(delta):
        if delta * sign <= 0:
            return None, None
        label = f"{metric} change" if isinstance(metric, str) else "change"
        return ctx.payload_evidence("delta_pct", label, IMPORTANCE_CORE, style="change"), metric
    move = largest_delta(ctx, sign=sign)
    if move is None:
        return None, None
    key, _ = move
    name = key.removesuffix("_pct")
    return ctx.merchant_evidence(f"performance.delta_7d.{key}", f"{name} change (7d)", IMPORTANCE_SUPPORT, style="change"), name


def _current_value(ctx: CandidateGenerationContext, metric: str | None) -> Evidence | None:
    if not metric or not _is_number(ctx.merchant.get("performance", {}).get(metric)):
        return None
    return ctx.merchant_evidence(f"performance.{metric}", f"{metric} (30d)", IMPORTANCE_CONTEXT)


def _member_evidence(ctx: CandidateGenerationContext) -> Evidence | None:
    for key, label, style in MEMBER_AGGREGATES:
        found = ctx.merchant_evidence(f"customer_aggregate.{key}", label, IMPORTANCE_SUPPORT, style=style)
        if found is not None:
            return found
    return None


def _offer_campaigns(ctx: CandidateGenerationContext, move: Evidence, objective: str, topic: frozenset[str]) -> list[Proposal]:
    return [
        Proposal(
            ActionType.DRAFT_CAMPAIGN,
            objective,
            CTAType.YES_NO,
            required=(move, offer_evidence(ctx, index)),
            topic=topic,
            selected_offer_id=offer["id"],
            assets=1,
        )
        for index, offer in ctx.active_offers
    ]


def perf_dip(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    move, metric = metric_move(ctx, -1)
    if move is None:
        return [no_action("the merchant's numbers show no dip to act on", *_any_moves(ctx))]

    topic = kind_topic(ctx, metric)
    baseline = ctx.payload_evidence("vs_baseline", "baseline", IMPORTANCE_SUPPORT)
    window = ctx.payload_evidence("window", "window", IMPORTANCE_CONTEXT)
    severity = (*signal_evidence(ctx, ["perf_dip_severe", "perf_dip_post_expiry"], "merchant signal"),)
    gaps = tuple(e for e in signal_evidence(ctx, LISTING_GAP_SIGNALS, "listing gap") if e is not None)
    seasonal = tuple(e for e in signals_with_prefix(ctx, "seasonal_dip", "merchant signal") if e is not None)

    proposals = [
        Proposal(
            ActionType.SEND_ALERT,
            "make the merchant aware of the drop before it compounds",
            CTAType.YES_NO,
            required=(move,),
            supporting=(baseline, window, _current_value(ctx, metric), *severity),
            topic=topic,
        )
    ]
    if gaps:
        proposals.append(
            Proposal(
                ActionType.RECOMMEND_OPERATIONAL_FIX,
                "fix the listing gaps most likely behind the drop",
                CTAType.YES_NO,
                required=(move, gaps[0]),
                supporting=gaps[1:],
                topic=topic,
                assets=len(gaps),
            )
        )
    proposals += _offer_campaigns(ctx, move, "recover demand by promoting an offer the merchant already runs", topic)
    if seasonal:
        proposals.append(no_action("the drop matches a known seasonal pattern for this merchant", move, *seasonal))
    return proposals


def _any_moves(ctx: CandidateGenerationContext) -> tuple[Evidence | None, ...]:
    return tuple(
        ctx.merchant_evidence(f"performance.delta_7d.{key}", f"{key.removesuffix('_pct')} change (7d)", IMPORTANCE_CONTEXT, style="change")
        for key in sorted((ctx.merchant.get("performance") or {}).get("delta_7d") or {})
    )


def perf_spike(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    move, metric = metric_move(ctx, 1)
    if move is None:
        return [no_action("the merchant's numbers show no spike to build on", *_any_moves(ctx))]

    topic = kind_topic(ctx, metric)
    driver = ctx.payload_evidence("likely_driver", "likely driver", IMPORTANCE_SUPPORT)
    baseline = ctx.payload_evidence("vs_baseline", "baseline", IMPORTANCE_SUPPORT)
    growth = signal_evidence(ctx, GROWTH_SIGNALS, "merchant signal")
    proposals = [
        Proposal(
            ActionType.SEND_INSIGHT,
            "share the upswing and what likely drove it",
            CTAType.NONE,
            required=(move,),
            supporting=(driver, baseline, _current_value(ctx, metric), *growth),
            topic=topic | tokens(driver.value if driver else None),
        )
    ]
    if driver is not None and "post" in tokens(driver.value):
        proposals.append(
            Proposal(
                ActionType.DRAFT_POST,
                "repeat the kind of post that drove the upswing",
                CTAType.YES_NO,
                required=(move, driver),
                topic=topic | tokens(driver.value),
                assets=1,
            )
        )
    proposals += _offer_campaigns(ctx, move, "ride the momentum with an offer the merchant already runs", topic)
    return proposals


def seasonal_perf_dip(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    if ctx.payload("is_expected_seasonal") is False:
        return perf_dip(ctx)
    move, metric = metric_move(ctx, -1)
    if move is None:
        return [no_action("the merchant's numbers show no dip to act on", *_any_moves(ctx))]

    seasonal = tuple(
        e
        for e in (
            ctx.payload_evidence("is_expected_seasonal", "expected seasonal dip", IMPORTANCE_CORE),
            current_seasonal_beat(ctx),
            *signals_with_prefix(ctx, "seasonal_dip", "merchant signal"),
        )
        if e is not None
    )
    if not seasonal:
        return perf_dip(ctx)

    note = ctx.payload_evidence("season_note", "season", IMPORTANCE_SUPPORT)
    members = _member_evidence(ctx)
    topic = kind_topic(ctx, metric, note.value if note else None)
    proposals = [
        Proposal(
            ActionType.SEND_INSIGHT,
            "reframe the dip as the expected seasonal lull so the merchant does not overreact",
            CTAType.NONE,
            required=(move, seasonal[0]),
            supporting=(*seasonal[1:], note),
            topic=topic,
        ),
        no_action("an expected seasonal dip needs no intervention", move, *seasonal, note),
    ]
    if members is not None:
        proposals.append(
            Proposal(
                ActionType.RECOMMEND_RETENTION,
                "use the seasonal lull to retain existing members instead of chasing acquisition",
                CTAType.YES_NO,
                required=(move, seasonal[0], members),
                supporting=(note,),
                topic=topic,
                assets=1,
            )
        )
    return proposals


def milestone_reached(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    metric = ctx.payload("metric")
    now = ctx.payload_evidence("value_now", f"{metric} now" if isinstance(metric, str) else "value now", IMPORTANCE_CORE)
    target = ctx.payload_evidence("milestone_value", "milestone", IMPORTANCE_CORE)
    if now is None or target is None or not (_is_number(now.value) and _is_number(target.value)):
        return [no_action("the milestone itself is not in the context")]

    imminent = ctx.payload_evidence("is_imminent", "imminent", IMPORTANCE_SUPPORT)
    topic = kind_topic(ctx, metric)
    proposals = [
        Proposal(
            ActionType.SEND_INSIGHT,
            "mark the merchant's milestone",
            CTAType.NONE,
            required=(now, target),
            supporting=(imminent,),
            topic=topic,
        )
    ]
    if now.value >= target.value:
        proposals.append(
            Proposal(
                ActionType.DRAFT_POST,
                "celebrate the milestone publicly",
                CTAType.YES_NO,
                required=(now, target),
                topic=topic,
                assets=1,
            )
        )
    elif "review" in tokens(metric):
        proposals.append(
            Proposal(
                ActionType.DRAFT_MESSAGE,
                "ask happy customers for the reviews that close the gap to the milestone",
                CTAType.YES_NO,
                required=(now, target),
                supporting=(imminent, top_review_theme(ctx, "pos")),
                topic=topic,
                assets=1,
            )
        )
    return proposals


GENERATOR = ArchetypeGenerator(
    TriggerArchetype.PERFORMANCE,
    {
        "perf_dip": perf_dip,
        "perf_spike": perf_spike,
        "seasonal_perf_dip": seasonal_perf_dip,
        "milestone_reached": milestone_reached,
    },
)
