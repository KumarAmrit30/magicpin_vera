"""CUSTOMER_TIMING: a customer-level window (recall, refill, lapse, appointment, trial, bridal).

Each trigger kind is a :class:`CustomerMoment`: which customer-facing action it
calls for and which trigger payload facts ground it. When the payload carries
none of those facts (e.g. generated placeholder triggers), the customer's own
relationship facts are the fallback grounding; with neither, nothing is sent.

Every moment also yields a merchant-facing ``draft_message`` (the merchant
approves the note before it goes out), and lapse moments yield
``recommend_retention`` when the merchant's aggregate shows a lapse pattern.
Consent, frequency and offer eligibility are deliberately not evaluated here.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial

from app.engine.actions import ActionType, CTAType
from app.engine.archetypes import TriggerArchetype
from app.engine.candidates.base import (
    IMPORTANCE_CONTEXT,
    IMPORTANCE_CORE,
    IMPORTANCE_SUPPORT,
    ArchetypeGenerator,
    Proposal,
    kind_topic,
    matching_offer,
    no_action,
    offer_evidence,
    parse_when,
)
from app.engine.candidates.context import CandidateGenerationContext
from app.engine.evidence import Evidence, EvidenceSource

RETENTION_AGGREGATES: tuple[tuple[str, str, str | None], ...] = (
    ("lapsed_180d_plus", "customers lapsed 180d+", None),
    ("lapsed_90d_plus", "customers lapsed 90d+", None),
    ("retention_6mo_pct", "6-month retention", "rate"),
    ("retention_3mo_pct", "3-month retention", "rate"),
    ("monthly_churn_pct", "monthly churn", "rate"),
)


@dataclass(frozen=True)
class CustomerMoment:
    action: ActionType
    objective: str
    cta_type: CTAType
    core: tuple[tuple[str, str], ...]
    """``(payload key, label)`` facts; at least one must be present (or the customer fallback)."""
    extra: tuple[tuple[str, str], ...] = ()
    options_key: str | None = None
    """Payload key holding concrete choices (slots); when present the CTA becomes a confirmation."""
    deadline_key: str | None = None
    offer_keys: tuple[str, ...] = ()
    """Payload keys whose text is matched against the merchant's active offers."""
    imminent: bool = False
    """The kind itself states the moment is within a day (``appointment_tomorrow``)."""
    lapse: bool = False


MOMENTS: dict[str, CustomerMoment] = {
    "recall_due": CustomerMoment(
        ActionType.SEND_CUSTOMER_REMINDER,
        "bring the customer back for their due recall visit",
        CTAType.YES_NO,
        core=(("service_due", "service due"), ("due_date", "due date")),
        extra=(("last_service_date", "last service"),),
        options_key="available_slots",
        deadline_key="due_date",
        offer_keys=("service_due",),
    ),
    "appointment_tomorrow": CustomerMoment(
        ActionType.SEND_CUSTOMER_REMINDER,
        "confirm the customer's appointment tomorrow",
        CTAType.CONFIRMATION,
        core=(("appointment_iso", "appointment"), ("service", "service")),
        imminent=True,
    ),
    "trial_followup": CustomerMoment(
        ActionType.SEND_CUSTOMER_FOLLOWUP,
        "convert the customer's trial into a next session",
        CTAType.YES_NO,
        core=(("trial_date", "trial date"),),
        options_key="next_session_options",
    ),
    "chronic_refill_due": CustomerMoment(
        ActionType.SEND_CUSTOMER_REMINDER,
        "refill the customer's chronic medication before stock runs out",
        CTAType.CONFIRMATION,
        core=(("molecule_list", "medicines"), ("stock_runs_out_iso", "stock runs out")),
        extra=(("last_refill", "last refill"), ("delivery_address_saved", "delivery address saved")),
        deadline_key="stock_runs_out_iso",
    ),
    "customer_lapsed_soft": CustomerMoment(
        ActionType.SEND_CUSTOMER_WINBACK,
        "win back a customer who has started to lapse",
        CTAType.YES_NO,
        core=(("days_since_last_visit", "days since last visit"),),
        extra=(("previous_focus", "previous focus"),),
        offer_keys=("previous_focus",),
        lapse=True,
    ),
    "customer_lapsed_hard": CustomerMoment(
        ActionType.SEND_CUSTOMER_WINBACK,
        "win back a long-lapsed customer without pressure",
        CTAType.YES_NO,
        core=(("days_since_last_visit", "days since last visit"),),
        extra=(("previous_focus", "previous focus"), ("previous_membership_months", "months as a member")),
        offer_keys=("previous_focus",),
        lapse=True,
    ),
    "wedding_package_followup": CustomerMoment(
        ActionType.SEND_CUSTOMER_FOLLOWUP,
        "move the bride-to-be into the next step of her wedding preparation",
        CTAType.YES_NO,
        core=(("days_to_wedding", "days to wedding"), ("wedding_date", "wedding date"), ("next_step_window_open", "next step")),
        extra=(("trial_completed", "trial completed"),),
        offer_keys=("next_step_window_open",),
    ),
}


def _customer_fallback(ctx: CandidateGenerationContext) -> tuple[Evidence | None, ...]:
    return (
        ctx.customer_evidence("relationship.last_visit", "last visit", IMPORTANCE_SUPPORT),
        ctx.customer_evidence("relationship.services_received", "services received", IMPORTANCE_CONTEXT),
    )


def _retention_evidence(ctx: CandidateGenerationContext) -> Evidence | None:
    for key, label, style in RETENTION_AGGREGATES:
        found = ctx.merchant_evidence(f"customer_aggregate.{key}", label, IMPORTANCE_SUPPORT, style=style)
        if found is not None:
            return found
    return None


def customer_moment(moment: CustomerMoment, ctx: CandidateGenerationContext) -> Iterable[Proposal]:
    if ctx.customer is None:
        scope = ctx.evidence(EvidenceSource.TRIGGER, "scope", "trigger scope", IMPORTANCE_CONTEXT)
        return [no_action("no customer is named, so there is nobody to contact", scope)]

    core = tuple(ctx.payload_evidence(key, label, IMPORTANCE_CORE) for key, label in moment.core)
    grounding = tuple(e for e in core if e is not None) or tuple(e for e in _customer_fallback(ctx) if e is not None)
    if not grounding:
        return [no_action("hold until the customer's timing can be grounded")]

    extra = tuple(ctx.payload_evidence(key, label, IMPORTANCE_SUPPORT) for key, label in moment.extra)
    options = ctx.payload_evidence(moment.options_key, "available options", IMPORTANCE_SUPPORT) if moment.options_key else None
    state = ctx.customer_evidence("state", "customer state", IMPORTANCE_CONTEXT)
    preferred = ctx.customer_evidence("preferences.preferred_slots", "preferred slots", IMPORTANCE_CONTEXT)
    last_visit = ctx.customer_evidence("relationship.last_visit", "last visit", IMPORTANCE_CONTEXT)
    offer = matching_offer(ctx, *(ctx.payload(key) for key in moment.offer_keys))
    offer_ev = offer_evidence(ctx, offer[0]) if offer else None
    deadline = parse_when(ctx.payload(moment.deadline_key)) if moment.deadline_key else None
    topic = kind_topic(ctx, *(e.value for e in grounding if isinstance(e.value, str)))
    shared = dict(
        required=(grounding[0],),
        supporting=(*grounding[1:], *extra, options, state, preferred, last_visit, offer_ev),
        topic=topic,
        selected_offer_id=offer[1]["id"] if offer else None,
        assets=int(options is not None) + int(offer_ev is not None),
        deadline=deadline,
        time_pressure_override=1.0 if moment.imminent else None,
    )

    proposals = [
        Proposal(
            moment.action,
            moment.objective,
            CTAType.CONFIRMATION if options is not None else moment.cta_type,
            **shared,
        ),
        Proposal(
            ActionType.DRAFT_MESSAGE,
            "prepare the customer message for the merchant to approve",
            CTAType.CONFIRMATION,
            **shared,
        ),
    ]
    retention = _retention_evidence(ctx) if moment.lapse else None
    if retention is not None:
        proposals.append(
            Proposal(
                ActionType.RECOMMEND_RETENTION,
                "address the wider lapse pattern this customer is part of",
                CTAType.YES_NO,
                required=(retention, grounding[0]),
                supporting=(state,),
                topic=topic,
            )
        )
    return proposals


GENERATOR = ArchetypeGenerator(
    TriggerArchetype.CUSTOMER_TIMING,
    {kind: partial(customer_moment, moment) for kind, moment in MOMENTS.items()},
)
