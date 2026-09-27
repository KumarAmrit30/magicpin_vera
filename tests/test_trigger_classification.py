"""Trigger kind -> archetype classification (Phase 2B)."""

import json

import pytest

from app.engine import (
    TRIGGER_KIND_ALIASES,
    TRIGGER_KIND_ARCHETYPES,
    TriggerArchetype,
    canonical_trigger_kind,
    classify_trigger,
    classify_trigger_kind,
)
from app.engine.candidates import GENERATORS
from tests.conftest import DATASET_DIR, requires_dataset

A = TriggerArchetype

SEED_KINDS = {
    "research_digest": A.MARKET_OPPORTUNITY,
    "regulation_change": A.SAFETY_COMPLIANCE,
    "recall_due": A.CUSTOMER_TIMING,
    "perf_dip": A.PERFORMANCE,
    "renewal_due": A.OPERATIONS,
    "festival_upcoming": A.MARKET_OPPORTUNITY,
    "wedding_package_followup": A.CUSTOMER_TIMING,
    "curious_ask_due": A.ACTIVE_INTENT,
    "winback_eligible": A.OPERATIONS,
    "ipl_match_today": A.MARKET_OPPORTUNITY,
    "review_theme_emerged": A.OPERATIONS,
    "milestone_reached": A.PERFORMANCE,
    "active_planning_intent": A.ACTIVE_INTENT,
    "seasonal_perf_dip": A.PERFORMANCE,
    "customer_lapsed_hard": A.CUSTOMER_TIMING,
    "trial_followup": A.CUSTOMER_TIMING,
    "supply_alert": A.SAFETY_COMPLIANCE,
    "chronic_refill_due": A.CUSTOMER_TIMING,
    "category_seasonal": A.MARKET_OPPORTUNITY,
    "gbp_unverified": A.OPERATIONS,
    "cde_opportunity": A.MARKET_OPPORTUNITY,
    "competitor_opened": A.COMPETITIVE,
    "perf_spike": A.PERFORMANCE,
    "dormant_with_vera": A.ACTIVE_INTENT,
}
GENERATED_ONLY_KINDS = {
    "appointment_tomorrow": A.CUSTOMER_TIMING,
    "customer_lapsed_soft": A.CUSTOMER_TIMING,
}
ALL_KINDS = {**SEED_KINDS, **GENERATED_ONLY_KINDS}

UNMAPPED_DOCUMENTED_KINDS = [
    "weather_heatwave",
    "local_news_event",
    "category_trend_movement",
    "scheduled_recurring",
    "unplanned_slot_open",
    "festival",
]


@pytest.mark.parametrize(("kind", "archetype"), sorted(ALL_KINDS.items()))
def test_every_dataset_kind_is_mapped(kind: str, archetype: TriggerArchetype) -> None:
    assert classify_trigger_kind(kind) is archetype
    assert classify_trigger({"kind": kind}) is archetype


def test_mapping_is_exactly_the_dataset_vocabulary() -> None:
    assert dict(TRIGGER_KIND_ARCHETYPES) == ALL_KINDS
    assert len(TRIGGER_KIND_ARCHETYPES) == 26


@requires_dataset
def test_seed_file_kinds_are_all_mapped() -> None:
    triggers = json.loads((DATASET_DIR / "triggers_seed.json").read_text())["triggers"]
    kinds = {t["kind"] for t in triggers}

    assert kinds == set(SEED_KINDS)
    assert all(classify_trigger(t) is not None for t in triggers)


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("research_digest_release", "research_digest"),
        ("category_research_digest_release", "research_digest"),
        ("bridal_followup", "wedding_package_followup"),
    ],
)
def test_documented_aliases_resolve(alias: str, canonical: str) -> None:
    assert canonical_trigger_kind(alias) == canonical
    assert classify_trigger_kind(alias) is ALL_KINDS[canonical]


def test_aliases_point_at_mapped_kinds() -> None:
    assert all(target in TRIGGER_KIND_ARCHETYPES for target in TRIGGER_KIND_ALIASES.values())
    assert not set(TRIGGER_KIND_ALIASES) & set(TRIGGER_KIND_ARCHETYPES)


@pytest.mark.parametrize(
    "kind",
    [*UNMAPPED_DOCUMENTED_KINDS, "made_up_kind", "", "PERF_DIP", "perf-dip", " perf_dip", "perf_dip ", "placeholder"],
)
def test_unknown_kinds_are_unmapped(kind: str) -> None:
    assert classify_trigger_kind(kind) is None
    assert canonical_trigger_kind(kind) is None
    assert classify_trigger({"kind": kind}) is None


@pytest.mark.parametrize("trigger", [{}, {"kind": None}, {"kind": 7}, {"kind": ["perf_dip"]}, {"id": "t", "payload": {}}])
def test_malformed_trigger_is_unmapped(trigger: dict) -> None:
    assert classify_trigger(trigger) is None


def test_every_archetype_has_kinds_and_a_generator() -> None:
    assert set(TRIGGER_KIND_ARCHETYPES.values()) == set(TriggerArchetype)
    assert set(GENERATORS) == set(TriggerArchetype)


@pytest.mark.parametrize("archetype", list(TriggerArchetype))
def test_generator_handles_exactly_its_mapped_kinds(archetype: TriggerArchetype) -> None:
    mapped = {kind for kind, a in TRIGGER_KIND_ARCHETYPES.items() if a is archetype}
    generator = GENERATORS[archetype]

    assert generator.archetype is archetype
    assert generator.kinds == mapped


def test_classification_is_deterministic() -> None:
    first = [classify_trigger_kind(k) for k in sorted(ALL_KINDS)]

    assert all([classify_trigger_kind(k) for k in sorted(ALL_KINDS)] == first for _ in range(5))
