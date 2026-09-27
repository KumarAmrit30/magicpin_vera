"""Shared fixtures: a controllable clock, a fresh app per test, and sample payloads.

Sample payloads are abbreviated from ``examples/api-call-examples.md``; the full
seed dataset is loaded from the vendored challenge package when present.
"""

import copy
import functools
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.engine.candidates import CandidateGenerationContext
from app.main import create_app
from app.state.container import StateContainer

DATASET_DIR = Path(__file__).resolve().parent.parent / "magicpin-ai-challenge" / "dataset"
T0 = datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC)


class FakeClock:
    """Deterministic wall clock and monotonic clock that only move when told to."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start
        self.elapsed = 0.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.elapsed += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def state(clock: FakeClock) -> StateContainer:
    return StateContainer.create(clock=clock, monotonic=clock.monotonic)


@pytest.fixture
def client(state: StateContainer) -> TestClient:
    app = create_app(settings=Settings(), state=state)
    return TestClient(app)


def category_payload(slug: str = "dentists") -> dict[str, Any]:
    return {
        "slug": slug,
        "voice": {"tone": "peer_clinical", "vocab_taboo": ["guaranteed", "100% safe"]},
        "offer_catalog": [
            {"id": "den_001", "title": "Dental Cleaning @ ₹299", "value": "299", "audience": "new_user", "type": "service_at_price"}
        ],
        "peer_stats": {"avg_rating": 4.4, "avg_ctr": 0.030},
        "digest": [
            {"id": "d_2026W17_jida_fluoride", "kind": "research", "title": "3-month fluoride recall cuts caries 38% better", "source": "JIDA Oct 2026, p.14"}
        ],
        "patient_content_library": [],
        "seasonal_beats": [{"month_range": "Nov-Feb", "note": "exam-stress bruxism spike"}],
        "trend_signals": [{"query": "clear aligners delhi", "delta_yoy": 0.62}],
    }


def merchant_payload(merchant_id: str = "m_001_drmeera_dentist_delhi", views: int = 2410) -> dict[str, Any]:
    return {
        "merchant_id": merchant_id,
        "category_slug": "dentists",
        "identity": {"name": "Dr. Meera's Dental Clinic", "city": "Delhi", "locality": "Lajpat Nagar",
                     "verified": True, "languages": ["en", "hi"], "owner_first_name": "Meera"},
        "subscription": {"status": "active", "plan": "Pro", "days_remaining": 82},
        "performance": {"window_days": 30, "views": views, "calls": 18, "directions": 45, "ctr": 0.021,
                        "delta_7d": {"views_pct": 0.18, "calls_pct": -0.05}},
        "offers": [{"id": "o_meera_001", "title": "Dental Cleaning @ ₹299", "status": "active"}],
        "conversation_history": [],
        "customer_aggregate": {"total_unique_ytd": 540, "lapsed_180d_plus": 78, "retention_6mo_pct": 0.38},
        "signals": ["stale_posts:22d", "ctr_below_peer_median", "high_risk_adult_cohort"],
    }


def customer_payload(customer_id: str = "c_001_priya_for_m001") -> dict[str, Any]:
    return {
        "customer_id": customer_id,
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "identity": {"name": "Priya", "phone_redacted": "<phone>", "language_pref": "hi-en mix"},
        "relationship": {"first_visit": "2025-11-04", "last_visit": "2026-05-12", "visits_total": 4,
                         "services_received": ["cleaning", "cleaning", "whitening", "cleaning"]},
        "state": "lapsed_soft",
        "preferences": {"preferred_slots": "weekday_evening", "channel": "whatsapp"},
        "consent": {"opted_in_at": "2025-11-04", "scope": ["recall_reminders", "appointment_reminders"]},
    }


def trigger_payload(trigger_id: str = "trg_001_research_digest_dentists") -> dict[str, Any]:
    return {
        "id": trigger_id,
        "scope": "merchant",
        "kind": "research_digest",
        "source": "external",
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "customer_id": None,
        "payload": {"category": "dentists", "top_item_id": "d_2026W17_jida_fluoride"},
        "urgency": 2,
        "suppression_key": "research:dentists:2026-W17",
        "expires_at": "2026-05-03T00:00:00Z",
    }


SAMPLE_PAYLOADS = {
    "category": ("dentists", category_payload),
    "merchant": ("m_001_drmeera_dentist_delhi", merchant_payload),
    "customer": ("c_001_priya_for_m001", customer_payload),
    "trigger": ("trg_001_research_digest_dentists", trigger_payload),
}


def context_body(
    scope: str,
    context_id: str,
    version: int,
    payload: dict[str, Any],
    delivered_at: str = "2026-04-26T09:45:00Z",
) -> dict[str, Any]:
    return {"scope": scope, "context_id": context_id, "version": version, "payload": payload, "delivered_at": delivered_at}


def reply_body(conversation_id: str = "conv_001", **overrides: Any) -> dict[str, Any]:
    body = {
        "conversation_id": conversation_id,
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "customer_id": None,
        "from_role": "merchant",
        "message": "Yes please send the abstract.",
        "received_at": "2026-04-26T10:42:00Z",
        "turn_number": 2,
    }
    body.update(overrides)
    return body


def load_seed_dataset() -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Return ``{scope: [(context_id, payload), ...]}`` from the official seed files."""
    categories = [json.loads(p.read_text()) for p in sorted((DATASET_DIR / "categories").glob("*.json"))]
    merchants = json.loads((DATASET_DIR / "merchants_seed.json").read_text())["merchants"]
    customers = json.loads((DATASET_DIR / "customers_seed.json").read_text())["customers"]
    triggers = json.loads((DATASET_DIR / "triggers_seed.json").read_text())["triggers"]
    return {
        "category": [(c["slug"], c) for c in categories],
        "merchant": [(m["merchant_id"], m) for m in merchants],
        "customer": [(c["customer_id"], c) for c in customers],
        "trigger": [(t["id"], t) for t in triggers],
    }


requires_dataset = pytest.mark.skipif(not DATASET_DIR.is_dir(), reason="official challenge dataset not vendored")


CANDIDATE_FEATURES = (
    "urgency",
    "time_pressure",
    "merchant_relevance",
    "conversation_relevance",
    "actionability",
    "evidence_strength",
    "engagement_potential",
)


def candidate_fields(**overrides: Any) -> dict[str, Any]:
    """Valid merchant-scoped DecisionCandidate fields; every feature defaults to 0.5."""
    fields: dict[str, Any] = {
        "trigger_id": "trg_001_research_digest_dentists",
        "archetype": "market_opportunity",
        "scope": "merchant",
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "objective": "share_research_digest",
        "action": "send_insight",
        "cta_type": "open_ended",
        "send_as": "vera",
        "suppression_key": "research:dentists:2026-W17",
        **dict.fromkeys(CANDIDATE_FEATURES, 0.5),
    }
    fields.update(overrides)
    return fields


SEED_NOW = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
"""Simulated decision time matching the seed dataset (IPL match day, Case Study 5)."""


@functools.cache
def _seed_index() -> dict[str, dict[str, dict[str, Any]]]:
    return {scope: dict(items) for scope, items in load_seed_dataset().items()}


def seed_context_parts(trigger_id: str) -> dict[str, Any]:
    """Deep copies of the category, merchant, trigger and customer payloads for a seed trigger."""
    index = _seed_index()
    trigger = index["trigger"][trigger_id]
    merchant = index["merchant"][trigger["merchant_id"]]
    customer_id = trigger.get("customer_id")
    return copy.deepcopy(
        {
            "category": index["category"][merchant["category_slug"]],
            "merchant": merchant,
            "trigger": trigger,
            "customer": index["customer"][customer_id] if customer_id else None,
        }
    )


def seed_context(trigger_id: str, **overrides: Any) -> CandidateGenerationContext:
    """A candidate-generation context for a seed trigger at :data:`SEED_NOW`."""
    fields = {**seed_context_parts(trigger_id), "now": SEED_NOW, **overrides}
    return CandidateGenerationContext(**fields)


def plan_fields(**overrides: Any) -> dict[str, Any]:
    """Valid merchant-scoped DecisionPlan fields (plan_id left to be derived)."""
    fields: dict[str, Any] = {
        "trigger_id": "trg_001_research_digest_dentists",
        "archetype": "market_opportunity",
        "scope": "merchant",
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "objective": "share_research_digest",
        "action": "send_insight",
        "suppression_key": "research:dentists:2026-W17",
        "cta_type": "open_ended",
        "send_as": "vera",
        "priority_score": 62.5,
        "confidence": 0.8,
    }
    fields.update(overrides)
    return fields
