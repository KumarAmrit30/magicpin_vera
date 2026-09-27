"""CandidateGenerationContext: validation, immutability, fact access (Phase 2B)."""

import copy
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.engine.candidates import CandidateGenerationContext
from app.engine.candidates.context import render_value
from app.engine.evidence import EvidenceSource
from app.state.conversation_store import Conversation
from tests.conftest import SEED_NOW, requires_dataset, seed_context, seed_context_parts

pytestmark = requires_dataset

MERCHANT_TRIGGER = "trg_004_perf_dip_bharat"
CUSTOMER_TRIGGER = "trg_003_recall_due_priya"


def conversation(*turns: tuple[str, str]) -> dict:
    ts = "2026-04-26T04:00:00Z"
    return {
        "conversation_id": "conv_1",
        "created_at": ts,
        "updated_at": ts,
        "turns": [
            {"role": role, "body": body, "sent_at": ts, "recorded_at": ts, "turn_number": i}
            for i, (role, body) in enumerate(turns, start=1)
        ],
    }


def test_builds_from_seed_payloads() -> None:
    ctx = seed_context(CUSTOMER_TRIGGER)

    assert ctx.trigger_id == CUSTOMER_TRIGGER
    assert ctx.kind == "recall_due"
    assert ctx.canonical_kind == "recall_due"
    assert ctx.merchant_id == "m_001_drmeera_dentist_delhi"
    assert ctx.customer_id == "c_001_priya_for_m001"
    assert ctx.urgency == 3
    assert ctx.expires_at is not None
    assert ctx.suppression_key


def test_is_frozen() -> None:
    ctx = seed_context(MERCHANT_TRIGGER)

    with pytest.raises(ValidationError):
        ctx.now = datetime(2027, 1, 1, tzinfo=UTC)


def test_payloads_are_deep_copied() -> None:
    parts = seed_context_parts(MERCHANT_TRIGGER)
    ctx = CandidateGenerationContext(**parts, now=SEED_NOW)
    parts["merchant"]["performance"]["delta_7d"]["calls_pct"] = 9.9
    parts["trigger"]["payload"]["delta_pct"] = 9.9

    assert ctx.merchant["performance"]["delta_7d"]["calls_pct"] == -0.5
    assert ctx.payload("delta_pct") == -0.5


def test_now_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        CandidateGenerationContext(**seed_context_parts(MERCHANT_TRIGGER), now=datetime(2026, 4, 26, 10, 0))


def test_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CandidateGenerationContext(**seed_context_parts(MERCHANT_TRIGGER), now=SEED_NOW, request={"x": 1})


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["merchant"].update(category_slug="gyms"), "does not match category"),
        (lambda p: p["trigger"].update(merchant_id="m_999"), "trigger merchant"),
        (lambda p: p["trigger"].update(customer_id="c_other"), "does not match trigger customer"),
        (lambda p: p["customer"].update(merchant_id="m_999"), "customer merchant"),
        (lambda p: p.update(customer=None), "no customer context was given"),
    ],
)
def test_rejects_inconsistent_contexts(mutate, message: str) -> None:
    parts = seed_context_parts(CUSTOMER_TRIGGER)
    mutate(parts)

    with pytest.raises(ValidationError, match=message):
        CandidateGenerationContext(**parts, now=SEED_NOW)


def test_rejects_customer_for_merchant_trigger() -> None:
    parts = seed_context_parts(MERCHANT_TRIGGER)
    parts["customer"] = seed_context_parts(CUSTOMER_TRIGGER)["customer"]

    with pytest.raises(ValidationError, match="does not match trigger customer"):
        CandidateGenerationContext(**parts, now=SEED_NOW)


@pytest.mark.parametrize("scope", ["category", "merchant", "trigger"])
def test_rejects_invalid_payloads(scope: str) -> None:
    parts = seed_context_parts(MERCHANT_TRIGGER)
    parts[scope] = {"unrelated": True}

    with pytest.raises(ValidationError):
        CandidateGenerationContext(**parts, now=SEED_NOW)


def test_alias_kind_is_canonicalised() -> None:
    parts = seed_context_parts("trg_007_bridal_followup_kavya")
    parts["trigger"]["kind"] = "bridal_followup"

    ctx = CandidateGenerationContext(**parts, now=SEED_NOW)

    assert ctx.kind == "bridal_followup"
    assert ctx.canonical_kind == "wedding_package_followup"


def test_suppression_key_falls_back_to_trigger_identity() -> None:
    parts = seed_context_parts(MERCHANT_TRIGGER)
    parts["trigger"].pop("suppression_key")

    ctx = CandidateGenerationContext(**parts, now=SEED_NOW)

    assert ctx.suppression_key == f"perf_dip:{MERCHANT_TRIGGER}"


def test_value_and_evidence_resolve_real_paths() -> None:
    ctx = seed_context(MERCHANT_TRIGGER)
    evidence = ctx.payload_evidence("delta_pct", "calls change", 0.9, style="change")

    assert ctx.value(EvidenceSource.MERCHANT, "performance.delta_7d.calls_pct") == -0.5
    assert evidence is not None
    assert evidence.field == "payload.delta_pct"
    assert evidence.value == -0.5
    assert evidence.formatted == "calls change: -50%"
    assert ctx.is_grounded(evidence)


@pytest.mark.parametrize("path", ["payload.nope", "offers.9.title", "identity.city.name"])
def test_missing_facts_give_no_evidence(path: str) -> None:
    ctx = seed_context(MERCHANT_TRIGGER)
    source = EvidenceSource.TRIGGER if path.startswith("payload") else EvidenceSource.MERCHANT

    assert ctx.value(source, path) is None
    assert ctx.evidence(source, path, "x", 0.5) is None


def test_empty_values_give_no_evidence_but_false_does() -> None:
    ctx = seed_context("trg_021_unverified_gbp_sunrise")

    assert ctx.merchant_evidence("offers", "offers", 0.5) is None
    verified = ctx.merchant_evidence("identity.verified", "verified", 0.5)
    assert verified is not None and verified.value is False
    assert verified.formatted == "verified: no"


def test_conversation_source_is_empty_without_conversation() -> None:
    ctx = seed_context(MERCHANT_TRIGGER)

    assert ctx.source_data(EvidenceSource.CONVERSATION) == {}
    assert ctx.source_data(EvidenceSource.CUSTOMER) == {}


def test_find_index_and_active_offers() -> None:
    ctx = seed_context("trg_006_festival_diwali")

    assert ctx.find_index(EvidenceSource.MERCHANT, "offers", "id", "o_studio11_002") == 1
    assert ctx.find_index(EvidenceSource.MERCHANT, "offers", "id", "missing") is None
    assert [offer["id"] for _, offer in ctx.active_offers] == ["o_studio11_001", "o_studio11_002"]


def test_signal_names_strip_values() -> None:
    ctx = seed_context("trg_001_research_digest_dentists")

    assert "stale_posts" in ctx.signal_names
    assert ctx.signal_index("stale_posts") == 0
    assert ctx.signal_index("absent") is None


def test_turns_combine_history_and_live_conversation() -> None:
    live = conversation(("merchant", "Can you send the list today?"))
    ctx = seed_context("trg_018_supply_atorvastatin_recall", conversation=live)

    assert [(t.source, t.field, t.role) for t in ctx.turns] == [
        (EvidenceSource.MERCHANT, "conversation_history.0", "vera"),
        (EvidenceSource.MERCHANT, "conversation_history.1", "merchant"),
        (EvidenceSource.CONVERSATION, "turns.0", "merchant"),
    ]
    assert ctx.turns[1].engagement == "intent_action"
    evidence = ctx.turn_evidence(ctx.turns[2], "merchant said", 0.5)
    assert evidence is not None and ctx.is_grounded(evidence)


def test_history_is_ordered_by_timestamp() -> None:
    parts = seed_context_parts("trg_018_supply_atorvastatin_recall")
    parts["merchant"]["conversation_history"].reverse()

    ctx = CandidateGenerationContext(**parts, now=SEED_NOW)

    assert [t.role for t in ctx.turns] == ["vera", "merchant"]
    assert ctx.turns[0].field == "conversation_history.1"


def test_accepts_conversation_model() -> None:
    model = Conversation.model_validate(conversation(("merchant", "hello")))

    ctx = seed_context(MERCHANT_TRIGGER, conversation=model)

    assert ctx.conversation is not None and ctx.conversation["turns"][0]["body"] == "hello"


def test_rejects_malformed_conversation() -> None:
    with pytest.raises(ValidationError):
        seed_context(MERCHANT_TRIGGER, conversation={"turns": "nope"})


@pytest.mark.parametrize(
    ("value", "style", "expected"),
    [
        (-0.5, "change", "-50%"),
        (0.15, "change", "+15%"),
        (0.021, "rate", "2.1%"),
        (True, None, "yes"),
        (["a", "b"], None, "a, b"),
        ([{"iso": "x", "label": "Wed 5 Nov, 6pm"}], None, "Wed 5 Nov, 6pm"),
        ({"b": 1, "a": 2}, None, "a=2, b=1"),
        (145, None, "145"),
    ],
)
def test_render_value(value, style, expected: str) -> None:
    assert render_value(value, style) == expected


def test_reading_does_not_mutate_payloads() -> None:
    ctx = seed_context("trg_018_supply_atorvastatin_recall")
    before = copy.deepcopy(ctx.model_dump())
    _ = (ctx.turns, ctx.active_offers, ctx.signal_names, ctx.payload("molecule"))

    assert ctx.model_dump() == before
