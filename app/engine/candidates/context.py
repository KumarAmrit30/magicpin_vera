"""Immutable, read-only input to candidate generation.

:class:`CandidateGenerationContext` holds deep copies of the raw context
payloads (so evidence can cite real field paths and be re-checked with
:func:`app.engine.evidence.is_grounded`), plus the simulated ``now`` used for
time pressure. It has no HTTP, FastAPI, or store dependencies.
"""

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, PrivateAttr, model_validator

from app.engine.archetypes import canonical_trigger_kind
from app.engine.evidence import Evidence, EvidenceSource, is_grounded, resolve_field
from app.models.domain import CategoryContext, CustomerContext, MerchantContext, TriggerContext
from app.state.conversation_store import Conversation

ValueStyle = Literal["change", "rate"] | None
"""How to render a numeric fraction: ``change`` -> ``-50%``, ``rate`` -> ``2.1%``."""

def render_value(value: Any, style: ValueStyle = None) -> str:
    """Human-readable rendering of a context value; never adds facts."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)) and style is not None:
        percent = value * 100
        return f"{percent:+.0f}%" if style == "change" else f"{percent:.1f}%"
    if isinstance(value, Mapping):
        label = value.get("label") or value.get("title") or value.get("iso")
        return str(label) if label is not None else ", ".join(f"{k}={value[k]}" for k in sorted(value))
    if isinstance(value, (list, tuple)):
        return ", ".join(render_value(item, style) for item in value)
    return str(value)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, tuple, dict)) and len(value) == 0)


@dataclass(frozen=True, slots=True)
class ConversationTurnView:
    """One conversation turn, with the path that grounds it."""

    source: EvidenceSource
    field: str
    """Path of the turn object inside ``source`` (e.g. ``conversation_history.1``)."""
    role: str
    body: str
    engagement: str | None


class CandidateGenerationContext(BaseModel):
    """Everything a generator may read. Frozen; payloads are deep-copied on construction.

    ``customer`` is required exactly when the trigger names a ``customer_id``.
    ``conversation`` is the live conversation for this merchant/customer, if any.
    ``now`` is the simulated decision time (the tick's ``now``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: dict[str, Any]
    merchant: dict[str, Any]
    trigger: dict[str, Any]
    customer: dict[str, Any] | None = None
    conversation: dict[str, Any] | None = None
    now: AwareDatetime

    _trigger_model: TriggerContext = PrivateAttr()

    @model_validator(mode="before")
    @classmethod
    def _copy_inputs(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        data = dict(data)
        conversation = data.get("conversation")
        if isinstance(conversation, Conversation):
            data["conversation"] = conversation.model_dump(mode="json")
        elif isinstance(conversation, Mapping):
            data["conversation"] = Conversation.model_validate(conversation).model_dump(mode="json")
        for key in ("category", "merchant", "trigger", "customer"):
            if isinstance(data.get(key), Mapping):
                data[key] = copy.deepcopy(dict(data[key]))
        return data

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        category = CategoryContext.model_validate(self.category)
        merchant = MerchantContext.model_validate(self.merchant)
        trigger = TriggerContext.model_validate(self.trigger)
        if merchant.category_slug != category.slug:
            raise ValueError(f"merchant category {merchant.category_slug!r} does not match category {category.slug!r}")
        if trigger.merchant_id is not None and trigger.merchant_id != merchant.merchant_id:
            raise ValueError(f"trigger merchant {trigger.merchant_id!r} does not match {merchant.merchant_id!r}")
        if self.customer is None:
            if trigger.customer_id is not None:
                raise ValueError(f"trigger names customer {trigger.customer_id!r} but no customer context was given")
        else:
            customer = CustomerContext.model_validate(self.customer)
            if customer.customer_id != trigger.customer_id:
                raise ValueError(f"customer {customer.customer_id!r} does not match trigger customer {trigger.customer_id!r}")
            if customer.merchant_id != merchant.merchant_id:
                raise ValueError(f"customer merchant {customer.merchant_id!r} does not match {merchant.merchant_id!r}")
        self._trigger_model = trigger
        return self

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #

    @property
    def trigger_id(self) -> str:
        return self._trigger_model.id

    @property
    def kind(self) -> str:
        return self._trigger_model.kind

    @property
    def canonical_kind(self) -> str | None:
        """Dataset kind after resolving documented aliases; ``None`` if unknown."""
        return canonical_trigger_kind(self.kind)

    @property
    def merchant_id(self) -> str:
        return self.merchant["merchant_id"]

    @property
    def customer_id(self) -> str | None:
        return None if self.customer is None else self.customer["customer_id"]

    @property
    def urgency(self) -> int | None:
        return self._trigger_model.urgency

    @property
    def expires_at(self) -> datetime | None:
        return self._trigger_model.expires_at

    @property
    def suppression_key(self) -> str:
        """The trigger's suppression key, or a stable per-trigger key when it has none."""
        return self._trigger_model.suppression_key or f"{self.kind}:{self.trigger_id}"

    # ------------------------------------------------------------------ #
    # Reading facts
    # ------------------------------------------------------------------ #

    def source_data(self, source: EvidenceSource) -> Mapping[str, Any]:
        """The raw payload behind an evidence source (empty when absent)."""
        data = {
            EvidenceSource.CATEGORY: self.category,
            EvidenceSource.MERCHANT: self.merchant,
            EvidenceSource.CUSTOMER: self.customer,
            EvidenceSource.TRIGGER: self.trigger,
            EvidenceSource.CONVERSATION: self.conversation,
        }[source]
        return data or {}

    def value(self, source: EvidenceSource, path: str) -> Any:
        """Value at ``path`` in ``source``; ``None`` when missing."""
        try:
            return resolve_field(self.source_data(source), path)
        except KeyError:
            return None

    def payload(self, key: str) -> Any:
        """Value of ``trigger.payload.<key>``; ``None`` when missing."""
        return self.value(EvidenceSource.TRIGGER, f"payload.{key}")

    def evidence(
        self, source: EvidenceSource, path: str, label: str, importance: float, *, style: ValueStyle = None
    ) -> Evidence | None:
        """Evidence citing the real value at ``path``; ``None`` when the fact is missing or empty."""
        value = self.value(source, path)
        if _is_empty(value):
            return None
        return Evidence(
            source=source,
            field=path,
            value=value,
            formatted=f"{label}: {render_value(value, style)}",
            importance=importance,
        )

    def payload_evidence(self, key: str, label: str, importance: float, *, style: ValueStyle = None) -> Evidence | None:
        return self.evidence(EvidenceSource.TRIGGER, f"payload.{key}", label, importance, style=style)

    def merchant_evidence(self, path: str, label: str, importance: float, *, style: ValueStyle = None) -> Evidence | None:
        return self.evidence(EvidenceSource.MERCHANT, path, label, importance, style=style)

    def customer_evidence(self, path: str, label: str, importance: float, *, style: ValueStyle = None) -> Evidence | None:
        return self.evidence(EvidenceSource.CUSTOMER, path, label, importance, style=style)

    def category_evidence(self, path: str, label: str, importance: float, *, style: ValueStyle = None) -> Evidence | None:
        return self.evidence(EvidenceSource.CATEGORY, path, label, importance, style=style)

    def find_index(self, source: EvidenceSource, list_path: str, key: str, value: Any) -> int | None:
        """Index of the first item in the list at ``list_path`` whose ``key`` equals ``value``."""
        items = self.value(source, list_path)
        if value is None or not isinstance(items, list):
            return None
        for index, item in enumerate(items):
            if isinstance(item, Mapping) and item.get(key) == value:
                return index
        return None

    def is_grounded(self, evidence: Evidence) -> bool:
        """True when ``evidence`` matches the value at its path in this context."""
        return is_grounded(evidence, self.source_data(evidence.source))

    # ------------------------------------------------------------------ #
    # Derived views
    # ------------------------------------------------------------------ #

    @cached_property
    def active_offers(self) -> tuple[tuple[int, Mapping[str, Any]], ...]:
        """``(index, offer)`` for each merchant offer with ``status == "active"``."""
        offers = self.merchant.get("offers") or []
        return tuple(
            (index, offer)
            for index, offer in enumerate(offers)
            if isinstance(offer, Mapping) and offer.get("status") == "active" and offer.get("id") and offer.get("title")
        )

    @cached_property
    def signal_names(self) -> frozenset[str]:
        """Merchant signal names without their ``:value`` suffix (``stale_posts:22d`` -> ``stale_posts``)."""
        return frozenset(str(s).split(":", 1)[0] for s in self.merchant.get("signals") or [] if isinstance(s, str))

    def signal_index(self, name: str) -> int | None:
        """Index in ``merchant.signals`` of the signal called ``name``."""
        for index, signal in enumerate(self.merchant.get("signals") or []):
            if isinstance(signal, str) and signal.split(":", 1)[0] == name:
                return index
        return None

    @cached_property
    def turns(self) -> tuple[ConversationTurnView, ...]:
        """Merchant conversation history (by timestamp), then live conversation turns (in order)."""
        history: list[tuple[datetime | None, int, ConversationTurnView]] = []
        for index, entry in enumerate(self.merchant.get("conversation_history") or []):
            if not isinstance(entry, Mapping) or not isinstance(entry.get("body"), str):
                continue
            view = ConversationTurnView(
                source=EvidenceSource.MERCHANT,
                field=f"conversation_history.{index}",
                role=str(entry.get("from") or ""),
                body=entry["body"],
                engagement=entry.get("engagement"),
            )
            history.append((_parse_timestamp(entry.get("ts")), index, view))
        history.sort(key=lambda item: (item[0] is None, item[0] or datetime.min, item[1]))
        live = [
            ConversationTurnView(
                source=EvidenceSource.CONVERSATION,
                field=f"turns.{index}",
                role=str(turn.get("role") or ""),
                body=turn["body"],
                engagement=None,
            )
            for index, turn in enumerate((self.conversation or {}).get("turns") or [])
            if isinstance(turn, Mapping) and isinstance(turn.get("body"), str)
        ]
        return tuple(view for _, _, view in history) + tuple(live)

    def turn_evidence(self, turn: ConversationTurnView, label: str, importance: float) -> Evidence | None:
        return self.evidence(turn.source, f"{turn.field}.body", label, importance)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
