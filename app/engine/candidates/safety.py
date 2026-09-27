"""SAFETY_COMPLIANCE: regulation changes and product/supply safety alerts.

Correctness before engagement: an alert is only proposed when the regulation or
the recalled product can be cited from the trigger or the category digest.
Without that core fact the only candidate is ``no_action``.
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
    digest_index,
    kind_topic,
    no_action,
    parse_when,
    signal_evidence,
)
from app.engine.candidates.context import CandidateGenerationContext


def regulation_change(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    index = digest_index(ctx, ctx.payload("top_item_id"))
    item = f"digest.{index}" if index is not None else None
    regulation = ctx.category_evidence(f"{item}.title", "regulation", IMPORTANCE_CORE) if item else None
    deadline = ctx.payload_evidence("deadline_iso", "compliance deadline", IMPORTANCE_SUPPORT)
    if regulation is None:
        return [no_action("hold until the regulation itself can be cited", deadline)]

    issuer = ctx.category_evidence(f"{item}.source", "issued by", IMPORTANCE_CONTEXT)
    summary = ctx.category_evidence(f"{item}.summary", "what changes", IMPORTANCE_SUPPORT)
    required_step = ctx.category_evidence(f"{item}.actionable", "required step", IMPORTANCE_SUPPORT)
    aware = signal_evidence(ctx, ["compliance_aware"], "merchant signal")
    topic = kind_topic(ctx, regulation.value)
    due = parse_when(ctx.payload("deadline_iso"))
    return [
        Proposal(
            ActionType.SEND_ALERT,
            "make sure the merchant knows the compliance change and its deadline",
            CTAType.YES_NO,
            required=(regulation,),
            supporting=(deadline, issuer, summary, *aware),
            topic=topic,
            deadline=due,
            assets=int(deadline is not None),
        ),
        Proposal(
            ActionType.RECOMMEND_OPERATIONAL_FIX,
            "get the practice compliant with the changed regulation before the deadline",
            CTAType.YES_NO,
            required=(regulation, required_step),
            supporting=(deadline, summary, *aware),
            topic=topic,
            deadline=due,
            assets=1,
        ),
    ]


def supply_alert(ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    molecule = ctx.payload_evidence("molecule", "recalled molecule", IMPORTANCE_CORE)
    batches = ctx.payload_evidence("affected_batches", "affected batches", IMPORTANCE_CORE)
    if molecule is None or batches is None:
        return [no_action("hold until the affected product and batches can be cited", molecule, batches)]

    manufacturer = ctx.payload_evidence("manufacturer", "manufacturer", IMPORTANCE_SUPPORT)
    index = digest_index(ctx, ctx.payload("alert_id"))
    notice = ctx.category_evidence(f"digest.{index}.title", "category alert", IMPORTANCE_SUPPORT) if index is not None else None
    details = ctx.category_evidence(f"digest.{index}.summary", "alert details", IMPORTANCE_SUPPORT) if index is not None else None
    chronic = ctx.merchant_evidence("customer_aggregate.chronic_rx_count", "chronic-Rx customers", IMPORTANCE_CONTEXT)
    topic = kind_topic(ctx, molecule.value, manufacturer.value if manufacturer else None)
    return [
        Proposal(
            ActionType.SEND_ALERT,
            "alert the merchant to the product recall affecting stock and customers",
            CTAType.YES_NO,
            required=(molecule, batches),
            supporting=(manufacturer, notice, details, chronic),
            topic=topic,
            assets=1,
        ),
        Proposal(
            ActionType.DRAFT_MESSAGE,
            "prepare the notification for chronic-Rx customers who may hold recalled batches",
            CTAType.YES_NO,
            required=(molecule, batches, chronic),
            supporting=(manufacturer, details),
            topic=topic,
            assets=2,
        ),
        Proposal(
            ActionType.ASK_MERCHANT,
            "confirm whether the affected batches are in the merchant's stock",
            CTAType.OPEN_ENDED,
            required=(molecule, batches),
            supporting=(manufacturer,),
            topic=topic,
        ),
    ]


GENERATOR = ArchetypeGenerator(
    TriggerArchetype.SAFETY_COMPLIANCE,
    {"regulation_change": regulation_change, "supply_alert": supply_alert},
)
