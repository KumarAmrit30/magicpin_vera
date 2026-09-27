"""Typed views of the four challenge context payloads.

Field names and types mirror ``challenge-brief.md`` §4 and the seed dataset in
``dataset/``. Every model allows extra keys, because category-specific fields
(e.g. ``customer_aggregate.chronic_rx_count`` for pharmacies) vary and the judge
injects post-submission context that may carry fields not modeled here.

Only the identifiers needed to join contexts together are required. Everything
else is optional so partial but well-typed payloads are accepted.
"""

from typing import Any

from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field

from app.models.enums import ContextScope, CustomerState, TriggerScope, TriggerSource


class DomainModel(BaseModel):
    """Base for payload models: tolerant of unknown keys, strict on known types."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --------------------------------------------------------------------------- #
# CategoryContext
# --------------------------------------------------------------------------- #


class VoiceProfile(DomainModel):
    tone: str | None = None
    register_: str | None = Field(
        default=None, validation_alias=AliasChoices("register", "register_"), serialization_alias="register"
    )
    code_mix: str | None = None
    vocab_allowed: list[str] = Field(default_factory=list)
    vocab_taboo: list[str] = Field(default_factory=list)
    salutation_examples: list[str] = Field(default_factory=list)
    tone_examples: list[str] = Field(default_factory=list)


class OfferTemplate(DomainModel):
    id: str | None = None
    title: str | None = None
    value: str | float | None = None
    audience: str | None = None
    type: str | None = None


class PeerStats(DomainModel):
    scope: str | None = None
    avg_rating: float | None = None
    avg_review_count: float | None = None
    avg_views_30d: float | None = None
    avg_calls_30d: float | None = None
    avg_directions_30d: float | None = None
    avg_ctr: float | None = None
    avg_photos: float | None = None
    avg_post_freq_days: float | None = None


class DigestItem(DomainModel):
    id: str | None = None
    kind: str | None = None
    title: str | None = None
    source: str | None = None
    summary: str | None = None
    date: str | None = None
    trial_n: int | None = None
    patient_segment: str | None = None
    actionable: str | None = None
    credits: int | None = None


class ContentItem(DomainModel):
    id: str | None = None
    title: str | None = None
    channel: str | None = None
    length_seconds: int | None = None
    body: str | None = None


class SeasonalBeat(DomainModel):
    month_range: str | None = None
    note: str | None = None


class TrendSignal(DomainModel):
    query: str | None = None
    delta_yoy: float | None = None
    segment_age: str | None = None
    skew: str | None = None


class CategoryContext(DomainModel):
    """Slow-changing knowledge pack about a business vertical."""

    slug: str = Field(min_length=1)
    display_name: str | None = None
    voice: VoiceProfile = Field(default_factory=VoiceProfile)
    offer_catalog: list[OfferTemplate] = Field(default_factory=list)
    peer_stats: PeerStats = Field(default_factory=PeerStats)
    digest: list[DigestItem] = Field(default_factory=list)
    patient_content_library: list[ContentItem] = Field(default_factory=list)
    seasonal_beats: list[SeasonalBeat] = Field(default_factory=list)
    trend_signals: list[TrendSignal] = Field(default_factory=list)
    regulatory_authorities: list[str] = Field(default_factory=list)
    professional_journals: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# MerchantContext
# --------------------------------------------------------------------------- #


class MerchantIdentity(DomainModel):
    name: str | None = None
    city: str | None = None
    locality: str | None = None
    place_id: str | None = None
    verified: bool | None = None
    languages: list[str] = Field(default_factory=list)
    owner_first_name: str | None = None
    established_year: int | None = None


class Subscription(DomainModel):
    status: str | None = None
    plan: str | None = None
    days_remaining: int | None = None
    days_since_expiry: int | None = None
    renewed_at: str | None = None


class PerformanceDelta(DomainModel):
    views_pct: float | None = None
    calls_pct: float | None = None
    ctr_pct: float | None = None


class PerformanceSnapshot(DomainModel):
    window_days: int | None = None
    views: int | None = None
    calls: int | None = None
    directions: int | None = None
    ctr: float | None = None
    leads: int | None = None
    delta_7d: PerformanceDelta = Field(default_factory=PerformanceDelta)


class MerchantOffer(DomainModel):
    id: str | None = None
    title: str | None = None
    status: str | None = None
    started: str | None = None
    ended: str | None = None


class ConversationHistoryEntry(DomainModel):
    ts: str | None = None
    from_: str | None = Field(default=None, validation_alias=AliasChoices("from", "from_"), serialization_alias="from")
    body: str | None = None
    engagement: str | None = None


class CustomerAggregate(DomainModel):
    """Category-specific counts; only the common field is typed explicitly."""

    total_unique_ytd: int | None = None


class ReviewTheme(DomainModel):
    theme: str | None = None
    sentiment: str | None = None
    occurrences_30d: int | None = None
    common_quote: str | None = None


class MerchantContext(DomainModel):
    """The current state of one merchant's business."""

    merchant_id: str = Field(min_length=1)
    category_slug: str = Field(min_length=1)
    identity: MerchantIdentity = Field(default_factory=MerchantIdentity)
    subscription: Subscription = Field(default_factory=Subscription)
    performance: PerformanceSnapshot = Field(default_factory=PerformanceSnapshot)
    offers: list[MerchantOffer] = Field(default_factory=list)
    conversation_history: list[ConversationHistoryEntry] = Field(default_factory=list)
    customer_aggregate: CustomerAggregate = Field(default_factory=CustomerAggregate)
    signals: list[str] = Field(default_factory=list)
    review_themes: list[ReviewTheme] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# CustomerContext
# --------------------------------------------------------------------------- #


class CustomerIdentity(DomainModel):
    name: str | None = None
    phone_redacted: str | None = None
    language_pref: str | None = None
    age_band: str | None = None


class Relationship(DomainModel):
    first_visit: str | None = None
    last_visit: str | None = None
    visits_total: int | None = None
    services_received: list[str] = Field(default_factory=list)
    lifetime_value: float | None = None


class Preferences(DomainModel):
    preferred_slots: str | None = None
    channel: str | None = None
    reminder_opt_in: bool | None = None


class Consent(DomainModel):
    opted_in_at: str | None = None
    scope: list[str] = Field(default_factory=list)


class CustomerContext(DomainModel):
    """One of a merchant's customers (only for customer-facing messages)."""

    customer_id: str = Field(min_length=1)
    merchant_id: str = Field(min_length=1)
    identity: CustomerIdentity = Field(default_factory=CustomerIdentity)
    relationship: Relationship = Field(default_factory=Relationship)
    state: CustomerState | None = None
    preferences: Preferences = Field(default_factory=Preferences)
    consent: Consent = Field(default_factory=Consent)


# --------------------------------------------------------------------------- #
# TriggerContext
# --------------------------------------------------------------------------- #


class TriggerContext(DomainModel):
    """The event that prompts a message right now."""

    id: str = Field(min_length=1)
    scope: TriggerScope
    kind: str = Field(min_length=1)
    source: TriggerSource | None = None
    merchant_id: str | None = None
    customer_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    urgency: int | None = Field(default=None, ge=1, le=5)
    suppression_key: str | None = None
    expires_at: AwareDatetime | None = None


# --------------------------------------------------------------------------- #
# Scope dispatch
# --------------------------------------------------------------------------- #

DomainContext = CategoryContext | MerchantContext | CustomerContext | TriggerContext

PAYLOAD_MODELS: dict[ContextScope, type[DomainContext]] = {
    ContextScope.CATEGORY: CategoryContext,
    ContextScope.MERCHANT: MerchantContext,
    ContextScope.CUSTOMER: CustomerContext,
    ContextScope.TRIGGER: TriggerContext,
}

PAYLOAD_ID_FIELDS: dict[ContextScope, str] = {
    ContextScope.CATEGORY: "slug",
    ContextScope.MERCHANT: "merchant_id",
    ContextScope.CUSTOMER: "customer_id",
    ContextScope.TRIGGER: "id",
}


def parse_context_payload(scope: ContextScope, payload: dict[str, Any]) -> DomainContext:
    """Validate ``payload`` against the typed model for ``scope``.

    Raises:
        pydantic.ValidationError: if the payload does not match the scope's schema.
    """
    return PAYLOAD_MODELS[scope].model_validate(payload)


def payload_identifier(scope: ContextScope, payload: dict[str, Any]) -> str | None:
    """Return the identifier embedded in ``payload`` for ``scope`` (e.g. ``merchant_id``), if any."""
    value = payload.get(PAYLOAD_ID_FIELDS[scope])
    return value if isinstance(value, str) else None
