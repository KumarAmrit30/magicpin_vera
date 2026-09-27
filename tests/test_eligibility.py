"""Phase 2C: candidate eligibility and suppression (unit and replay tests).

Every test is pure domain code: contexts are built from the vendored seed
dataset, suppression is a bare :class:`SuppressionStore`, and FastAPI is never
started.
"""

import ast
import copy
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.engine import ActionType, CTAType, DecisionCandidate, DecisionScope, Evidence, EvidenceSource
from app.engine import eligibility as eligibility_module
from app.engine.candidates import CandidateGenerationContext, generate_candidates
from app.engine.eligibility import (
    REASON_SOURCES,
    UNANSWERED_NUDGE_LIMIT,
    EligibilityReason,
    EligibilityReasonCode,
    EligibilityResult,
    eligible_candidates,
    evaluate_candidates,
    evaluate_eligibility,
    merchant_suppression_key,
)
from app.state.suppression_store import SuppressionRecord, SuppressionStore
from tests.conftest import SEED_NOW, requires_dataset, seed_context_parts

pytestmark = requires_dataset

A = ActionType
R = EligibilityReasonCode

DIGEST = "trg_001_research_digest_dentists"
COMPLIANCE = "trg_002_compliance_dci_radiograph"
RECALL = "trg_003_recall_due_priya"
RENEWAL = "trg_005_renewal_due_bharat"
PERF_DIP = "trg_004_perf_dip_bharat"
FESTIVAL = "trg_006_festival_diwali"
MERCHANT_WINBACK = "trg_009_winback_glamour"
IPL = "trg_010_ipl_match_delhi"
SEASONAL = "trg_014_seasonal_acquisition_dip_powerhouse"
CUSTOMER_WINBACK = "trg_015_winback_rashmi"
GBP = "trg_021_unverified_gbp_sunrise"

M_MEERA = "m_001_drmeera_dentist_delhi"
M_BHARAT = "m_002_bharat_dentist_mumbai"
C_PRIYA = "c_001_priya_for_m001"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def parts(trigger_id: str) -> dict[str, Any]:
    return seed_context_parts(trigger_id)


def build(p: dict[str, Any], **overrides: Any) -> CandidateGenerationContext:
    return CandidateGenerationContext(**{**p, "now": SEED_NOW, **overrides})


def ctx(trigger_id: str, **overrides: Any) -> CandidateGenerationContext:
    return build(parts(trigger_id), **overrides)


def candidates(context: CandidateGenerationContext) -> list[DecisionCandidate]:
    return generate_candidates(context)


def pick(context: CandidateGenerationContext, action: ActionType) -> DecisionCandidate:
    return next(c for c in candidates(context) if c.action is action)


def store() -> SuppressionStore:
    return SuppressionStore(clock=lambda: SEED_NOW)


def evaluate(candidate: DecisionCandidate, context: CandidateGenerationContext, suppression: SuppressionStore | None = None, now: datetime = SEED_NOW) -> EligibilityResult:
    return evaluate_eligibility(candidate, context, suppression if suppression is not None else store(), now=now)


def codes(result: EligibilityResult) -> list[EligibilityReasonCode]:
    return list(result.reason_codes)


def set_path(data: Any, path: str, value: Any) -> None:
    *head, last = path.split(".")
    for key in head:
        data = data[int(key)] if isinstance(data, list) else data[key]
    if isinstance(data, list):
        data[int(last)] = value
    else:
        data[last] = value


def del_path(data: Any, path: str) -> None:
    *head, last = path.split(".")
    for key in head:
        data = data[int(key)] if isinstance(data, list) else data[key]
    del data[int(last) if isinstance(data, list) else last]


def conversation(
    merchant_id: str | None,
    customer_id: str | None = None,
    *,
    state: str = "new",
    turns: tuple[tuple[str, str], ...] = (),
    conversation_id: str = "conv_1",
) -> dict[str, Any]:
    ts = SEED_NOW.isoformat()
    return {
        "conversation_id": conversation_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "trigger_id": None,
        "state": state,
        "turns": [{"role": role, "body": body, "sent_at": ts, "recorded_at": ts} for role, body in turns],
        "created_at": ts,
        "updated_at": ts,
    }


def cited_path(candidate: DecisionCandidate, source: EvidenceSource) -> Evidence:
    return next(e for e in candidate.evidence if e.source is source)


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #


def test_valid_candidate_is_eligible_with_no_reasons() -> None:
    context = ctx(DIGEST)
    result = evaluate(pick(context, A.SEND_INSIGHT), context)

    assert result.eligible is True
    assert result.reasons == ()


def test_result_keeps_the_candidate_unchanged() -> None:
    context = ctx(DIGEST)
    candidate = pick(context, A.SEND_INSIGHT)

    assert evaluate(candidate, context).candidate == candidate


@pytest.mark.parametrize("eligible, reasons", [(True, (EligibilityReason(code=R.SUPPRESSED, detail="x"),)), (False, ())])
def test_result_verdict_must_match_reasons(eligible: bool, reasons: tuple) -> None:
    context = ctx(DIGEST)
    with pytest.raises(ValueError, match="eligible must be true exactly"):
        EligibilityResult(candidate=pick(context, A.SEND_INSIGHT), eligible=eligible, reasons=reasons)


def test_result_and_reasons_are_frozen() -> None:
    reason = EligibilityReason(code=R.SUPPRESSED, detail="key=k")

    with pytest.raises(ValueError):
        reason.code = R.TRIGGER_EXPIRED  # type: ignore[misc]


def test_every_reason_code_has_a_source() -> None:
    assert set(REASON_SOURCES) == set(EligibilityReasonCode)
    assert all(REASON_SOURCES[code] for code in EligibilityReasonCode)


def test_reason_source_defaults_from_the_code() -> None:
    reason = EligibilityReason(code=R.TRIGGER_EXPIRED, detail="d")

    assert reason.source == REASON_SOURCES[R.TRIGGER_EXPIRED]


def test_reason_codes_are_stable_strings() -> None:
    assert [c.value for c in EligibilityReasonCode] == [
        "structure_invalid",
        "trigger_mismatch",
        "trigger_changed",
        "merchant_mismatch",
        "customer_not_in_context",
        "conversation_mismatch",
        "evidence_missing",
        "evidence_not_grounded",
        "trigger_expired",
        "offer_unavailable",
        "merchant_state_conflict",
        "conversation_closed",
        "nudge_limit_reached",
        "suppressed",
        "merchant_suppressed",
    ]


# --------------------------------------------------------------------------- #
# Structural (Phase 2A invariants are re-validated, not re-implemented)
# --------------------------------------------------------------------------- #


def _constructed(candidate: DecisionCandidate, **changes: Any) -> DecisionCandidate:
    return DecisionCandidate.model_construct(**{**dict(candidate), **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"action": A.NO_ACTION, "cta_type": CTAType.YES_NO},  # no_action with a CTA
        {"scope": DecisionScope.CUSTOMER},  # customer scope without customer_id
        {"send_as": "merchant_on_behalf"},  # send_as inconsistent with scope
        {"urgency": 1.5},  # feature out of range
    ],
)
def test_structurally_invalid_candidate_is_rejected_alone(changes: dict[str, Any]) -> None:
    context = ctx(DIGEST)
    broken = _constructed(pick(context, A.SEND_INSIGHT), **changes)

    result = evaluate(broken, context)

    assert codes(result) == [R.STRUCTURE_INVALID]
    assert result.reasons[0].detail


def test_customer_action_outside_customer_scope_is_structurally_invalid() -> None:
    context = ctx(RECALL)
    broken = _constructed(pick(context, A.SEND_CUSTOMER_REMINDER), scope=DecisionScope.MERCHANT, send_as="vera")

    assert codes(evaluate(broken, context)) == [R.STRUCTURE_INVALID]


def test_non_candidate_input_is_a_programming_error() -> None:
    with pytest.raises(TypeError):
        evaluate_eligibility({"trigger_id": DIGEST}, ctx(DIGEST), store(), now=SEED_NOW)  # type: ignore[arg-type]


def test_naive_evaluation_time_is_rejected() -> None:
    context = ctx(DIGEST)
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate(pick(context, A.SEND_INSIGHT), context, now=SEED_NOW.replace(tzinfo=None))


def test_now_is_required_keyword() -> None:
    context = ctx(DIGEST)
    with pytest.raises(TypeError):
        evaluate_eligibility(pick(context, A.SEND_INSIGHT), context, store())  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Trigger consistency and scope integrity
# --------------------------------------------------------------------------- #


def test_candidate_for_another_trigger_is_rejected() -> None:
    digest = pick(ctx(DIGEST), A.SEND_INSIGHT)

    result = evaluate(digest, ctx(COMPLIANCE))

    assert R.TRIGGER_MISMATCH in codes(result)
    assert R.TRIGGER_CHANGED not in codes(result)


def test_candidate_for_another_merchant_is_rejected() -> None:
    context = ctx(DIGEST)
    moved = pick(context, A.SEND_INSIGHT).model_copy(update={"merchant_id": M_BHARAT})

    assert R.MERCHANT_MISMATCH in codes(evaluate(moved, context))


@pytest.mark.parametrize(
    "field, value, changed",
    [
        ("suppression_key", "research:dentists:2026-W18", "suppression_key"),
        ("expires_at", "2026-05-10T00:00:00Z", "expires_at"),
        ("kind", "perf_dip", "archetype"),
    ],
)
def test_changed_trigger_version_is_rejected(field: str, value: str, changed: str) -> None:
    candidate = pick(ctx(DIGEST), A.SEND_INSIGHT)
    p = parts(DIGEST)
    p["trigger"][field] = value

    result = evaluate(candidate, build(p))

    assert R.TRIGGER_CHANGED in codes(result)
    assert changed in next(r.detail for r in result.reasons if r.code is R.TRIGGER_CHANGED)


def test_customer_candidate_needs_the_customer_in_context() -> None:
    candidate = pick(ctx(RECALL), A.SEND_CUSTOMER_REMINDER)
    p = parts(RECALL)
    p["trigger"].pop("customer_id")
    p["customer"] = None

    result = evaluate(candidate, build(p))

    assert R.CUSTOMER_NOT_IN_CONTEXT in codes(result)
    assert R.EVIDENCE_NOT_GROUNDED in codes(result)  # customer facts can no longer be re-checked


def test_customer_candidate_for_another_customer_is_rejected() -> None:
    context = ctx(RECALL)
    other = pick(context, A.SEND_CUSTOMER_REMINDER).model_copy(update={"customer_id": "c_002_rahul_for_m001"})

    assert R.CUSTOMER_NOT_IN_CONTEXT in codes(evaluate(other, context))


def test_merchant_scoped_retention_may_reference_the_context_customer() -> None:
    context = ctx(CUSTOMER_WINBACK)
    retention = [c for c in candidates(context) if c.action is A.RECOMMEND_RETENTION]

    assert retention
    for candidate in retention:
        assert candidate.scope is DecisionScope.MERCHANT
        assert evaluate(candidate, context).eligible


def test_conversation_for_another_merchant_is_rejected() -> None:
    context = ctx(DIGEST, conversation=conversation(M_BHARAT))

    assert R.CONVERSATION_MISMATCH in codes(evaluate(pick(context, A.SEND_INSIGHT), context))


def test_conversation_for_another_customer_is_rejected() -> None:
    context = ctx(RECALL, conversation=conversation(M_MEERA, "c_999_someone"))

    assert R.CONVERSATION_MISMATCH in codes(evaluate(pick(context, A.SEND_CUSTOMER_REMINDER), context))


# --------------------------------------------------------------------------- #
# Evidence re-grounding against the current context
# --------------------------------------------------------------------------- #


def test_changed_evidence_value_is_rejected() -> None:
    candidate = pick(ctx(DIGEST), A.SEND_INSIGHT)
    fact = cited_path(candidate, EvidenceSource.MERCHANT)
    p = parts(DIGEST)
    set_path(p["merchant"], fact.field, "changed")

    result = evaluate(candidate, build(p))

    assert codes(result) == [R.EVIDENCE_NOT_GROUNDED]
    assert f"merchant:{fact.field}" in result.reasons[0].detail


def test_removed_evidence_field_is_rejected() -> None:
    candidate = pick(ctx(DIGEST), A.SEND_INSIGHT)
    fact = cited_path(candidate, EvidenceSource.MERCHANT)
    p = parts(DIGEST)
    del_path(p["merchant"], fact.field)

    assert R.EVIDENCE_NOT_GROUNDED in codes(evaluate(candidate, build(p)))


def test_evidence_with_the_wrong_type_is_rejected() -> None:
    candidate = pick(ctx(DIGEST), A.SEND_INSIGHT)
    fact = next(e for e in candidate.evidence if isinstance(e.value, int) and not isinstance(e.value, bool))
    p = parts(DIGEST)
    set_path(p[fact.source.value], fact.field, str(fact.value))

    assert R.EVIDENCE_NOT_GROUNDED in codes(evaluate(candidate, build(p)))


def test_fabricated_evidence_is_rejected_and_named() -> None:
    context = ctx(DIGEST)
    candidate = pick(context, A.SEND_INSIGHT)
    fake = Evidence(source=EvidenceSource.MERCHANT, field="performance.views", value=999_999, formatted="views: 999999", importance=0.9)
    padded = candidate.model_copy(update={"evidence": (*candidate.evidence, fake)})

    result = evaluate(padded, context)

    assert codes(result) == [R.EVIDENCE_NOT_GROUNDED]
    assert result.reasons[0].detail == "merchant:performance.views"


def test_sendable_candidate_without_evidence_is_rejected() -> None:
    context = ctx(DIGEST)
    bare = pick(context, A.SEND_INSIGHT).model_copy(update={"evidence": ()})

    assert codes(evaluate(bare, context)) == [R.EVIDENCE_MISSING]


def test_no_action_without_evidence_is_allowed() -> None:
    context = ctx(SEASONAL)
    restraint = pick(context, A.NO_ACTION).model_copy(update={"evidence": ()})

    assert evaluate(restraint, context).eligible


def test_evaluation_does_not_mutate_candidate_or_context() -> None:
    context = ctx(DIGEST, conversation=conversation(M_MEERA, turns=(("vera", "hi"),)))
    candidate = pick(context, A.SEND_INSIGHT)
    before = (candidate.model_dump(), copy.deepcopy(context.model_dump()))

    evaluate(candidate, context)
    evaluate(candidate, context, now=SEED_NOW + timedelta(days=365))

    assert (candidate.model_dump(), context.model_dump()) == before


# --------------------------------------------------------------------------- #
# Freshness (trigger expires_at; explicit evaluation time)
# --------------------------------------------------------------------------- #


def _expiry(trigger_id: str) -> datetime:
    return ctx(trigger_id).expires_at  # type: ignore[return-value]


@pytest.mark.parametrize(
    "offset, expired",
    [(-timedelta(hours=1), False), (timedelta(0), False), (timedelta(microseconds=1), True), (timedelta(days=30), True)],
)
def test_trigger_expiry_boundary(offset: timedelta, expired: bool) -> None:
    context = ctx(IPL)
    now = _expiry(IPL) + offset

    results = evaluate_candidates(candidates(context), context, store(), now=now)

    assert all((R.TRIGGER_EXPIRED in r.reason_codes) is expired for r in results)


def test_expired_detail_names_the_deadline_and_now() -> None:
    context = ctx(IPL)
    now = _expiry(IPL) + timedelta(minutes=1)

    result = evaluate(pick(context, A.SEND_INSIGHT), context, now=now)

    detail = next(r.detail for r in result.reasons if r.code is R.TRIGGER_EXPIRED)
    assert _expiry(IPL).isoformat() in detail and now.isoformat() in detail


def test_evaluation_uses_the_explicit_now_not_the_context_time() -> None:
    context = ctx(DIGEST)  # context.now is SEED_NOW, well before expiry
    late = _expiry(DIGEST) + timedelta(days=1)

    assert R.TRIGGER_EXPIRED in codes(evaluate(pick(context, A.SEND_INSIGHT), context, now=late))


def test_trigger_without_expiry_never_expires() -> None:
    p = parts(DIGEST)
    p["trigger"]["expires_at"] = None
    context = build(p)

    assert evaluate(pick(context, A.SEND_INSIGHT), context, now=SEED_NOW + timedelta(days=3650)).eligible


def test_no_action_is_not_subject_to_expiry() -> None:
    context = ctx(SEASONAL)

    assert evaluate(pick(context, A.NO_ACTION), context, now=_expiry(SEASONAL) + timedelta(days=1)).eligible


def test_appointment_style_customer_trigger_stops_after_its_deadline() -> None:
    context = ctx("trg_019_chronic_refill_grandfather")  # expires when the stock runs out
    reminder = pick(context, A.SEND_CUSTOMER_REMINDER)
    deadline = context.expires_at
    assert deadline is not None

    assert evaluate(reminder, context, now=deadline - timedelta(hours=1)).eligible
    assert R.TRIGGER_EXPIRED in codes(evaluate(reminder, context, now=deadline + timedelta(hours=1)))


# --------------------------------------------------------------------------- #
# Merchant state premises
# --------------------------------------------------------------------------- #


def test_gbp_candidate_is_rejected_once_the_listing_is_verified() -> None:
    fix = pick(ctx(GBP), A.RECOMMEND_OPERATIONAL_FIX)
    p = parts(GBP)
    p["merchant"].setdefault("identity", {})["verified"] = True

    result = evaluate(fix, build(p))

    assert R.MERCHANT_STATE_CONFLICT in codes(result)
    assert "identity.verified=true" in next(r.detail for r in result.reasons if r.code is R.MERCHANT_STATE_CONFLICT)


def test_renewal_candidate_is_rejected_when_the_subscription_is_no_longer_renewable() -> None:
    alert = pick(ctx(RENEWAL), A.SEND_ALERT)
    p = parts(RENEWAL)
    p["merchant"]["subscription"]["status"] = "expired"

    assert R.MERCHANT_STATE_CONFLICT in codes(evaluate(alert, build(p)))


def test_merchant_winback_is_rejected_once_the_subscription_is_active() -> None:
    ask = pick(ctx(MERCHANT_WINBACK), A.ASK_MERCHANT)
    p = parts(MERCHANT_WINBACK)
    p["merchant"]["subscription"]["status"] = "active"

    assert R.MERCHANT_STATE_CONFLICT in codes(evaluate(ask, build(p)))


def _offer_index(p: dict[str, Any], offer_id: str) -> int:
    return next(i for i, o in enumerate(p["merchant"]["offers"]) if o["id"] == offer_id)


def test_candidate_is_rejected_when_its_offer_expires() -> None:
    reminder = pick(ctx(RECALL), A.SEND_CUSTOMER_REMINDER)
    p = parts(RECALL)
    p["merchant"]["offers"][_offer_index(p, reminder.selected_offer_id)]["status"] = "expired"

    result = evaluate(reminder, build(p))

    assert R.OFFER_UNAVAILABLE in codes(result)
    assert f"selected_offer_id={reminder.selected_offer_id}" in next(r.detail for r in result.reasons if r.code is R.OFFER_UNAVAILABLE)


def test_candidate_is_rejected_when_its_offer_is_removed() -> None:
    reminder = pick(ctx(RECALL), A.SEND_CUSTOMER_REMINDER)
    p = parts(RECALL)
    del p["merchant"]["offers"][_offer_index(p, reminder.selected_offer_id)]

    assert R.OFFER_UNAVAILABLE in codes(evaluate(reminder, build(p)))


def test_per_offer_campaign_drafts_stay_separate_and_independent() -> None:
    context = ctx(FESTIVAL)
    drafts = [c for c in candidates(context) if c.action is A.DRAFT_CAMPAIGN]
    assert len(drafts) == 2 and drafts[0].selected_offer_id != drafts[1].selected_offer_id

    results = evaluate_candidates(drafts, context, store(), now=SEED_NOW)
    assert [r.candidate for r in results] == drafts and all(r.eligible for r in results)

    p = parts(FESTIVAL)
    p["merchant"]["offers"][_offer_index(p, drafts[0].selected_offer_id)]["status"] = "expired"
    first, second = evaluate_candidates(drafts, build(p), store(), now=SEED_NOW)
    assert R.OFFER_UNAVAILABLE in first.reason_codes
    assert R.OFFER_UNAVAILABLE not in second.reason_codes


def test_unrelated_merchant_change_does_not_reject() -> None:
    candidate = pick(ctx(DIGEST), A.SEND_INSIGHT)
    cited = {e.field.split(".")[0] for e in candidate.evidence if e.source is EvidenceSource.MERCHANT}
    p = parts(DIGEST)
    p["merchant"]["unrelated_note"] = "irrelevant"
    assert "unrelated_note" not in cited

    assert evaluate(candidate, build(p)).eligible


# --------------------------------------------------------------------------- #
# Customer state (only evidence re-grounding; no invented consent/state gates)
# --------------------------------------------------------------------------- #


def test_changed_customer_fact_is_rejected() -> None:
    winback = pick(ctx(CUSTOMER_WINBACK), A.SEND_CUSTOMER_WINBACK)
    fact = cited_path(winback, EvidenceSource.CUSTOMER)
    p = parts(CUSTOMER_WINBACK)
    set_path(p["customer"], fact.field, "changed")

    assert R.EVIDENCE_NOT_GROUNDED in codes(evaluate(winback, build(p)))


def test_uncited_customer_state_and_consent_do_not_gate_eligibility() -> None:
    winback = pick(ctx(CUSTOMER_WINBACK), A.SEND_CUSTOMER_WINBACK)
    cited = {e.field.split(".")[0] for e in winback.evidence if e.source is EvidenceSource.CUSTOMER}
    p = parts(CUSTOMER_WINBACK)
    for key in ("consent", "preferences", "state"):
        if key not in cited:
            p["customer"][key] = {} if key != "state" else "churned"

    assert evaluate(winback, build(p)).eligible


# --------------------------------------------------------------------------- #
# Conversation (state set by the reply handler; eligibility never parses text)
# --------------------------------------------------------------------------- #


def _merchant_candidate(conv: dict[str, Any], trigger_id: str = DIGEST, action: ActionType = A.SEND_INSIGHT) -> EligibilityResult:
    context = ctx(trigger_id, conversation=conv)
    return evaluate(pick(context, action), context)


@pytest.mark.parametrize(
    "state, turns",
    [
        ("qualifying", (("vera", "JIDA digest ..."), ("merchant", "Sounds useful, tell me more"))),
        ("committed", (("vera", "Shall I draft it?"), ("merchant", "Yes, let's do it"))),
        ("waiting", (("vera", "Shall I draft it?"), ("merchant", "Can you also check my GST filing?"))),
    ],
    ids=["normal-reply", "planning-intent", "unrelated-question"],
)
def test_open_conversation_does_not_block(state: str, turns: tuple) -> None:
    assert _merchant_candidate(conversation(M_MEERA, state=state, turns=turns)).eligible


@pytest.mark.parametrize(
    "state, reply",
    [("ended", "Not interested. Stop messaging me."), ("ended", "Stop messaging me. This is useless spam."), ("completed", "Done, thanks")],
    ids=["rejection", "hostile", "completed"],
)
def test_closed_conversation_blocks_sends_into_it(state: str, reply: str) -> None:
    result = _merchant_candidate(conversation(M_MEERA, state=state, turns=(("vera", "hi"), ("merchant", reply))))

    assert codes(result) == [R.CONVERSATION_CLOSED]
    assert "conv_1" in result.reasons[0].detail


def test_closed_conversation_allows_no_action() -> None:
    conv = conversation("m_007_powerhouse_gym_bangalore", state="ended")

    assert _merchant_candidate(conv, SEASONAL, A.NO_ACTION).eligible


def test_closed_customer_thread_blocks_only_customer_sends() -> None:
    context = ctx(RECALL, conversation=conversation(M_MEERA, C_PRIYA, state="ended"))
    by_action = {r.candidate.action: r for r in evaluate_candidates(candidates(context), context, store(), now=SEED_NOW)}

    assert codes(by_action[A.SEND_CUSTOMER_REMINDER]) == [R.CONVERSATION_CLOSED]
    assert by_action[A.DRAFT_MESSAGE].eligible  # merchant-facing: not sent into the customer's thread


def test_closed_merchant_thread_does_not_block_customer_sends() -> None:
    context = ctx(RECALL, conversation=conversation(M_MEERA, state="ended"))
    by_action = {r.candidate.action: r for r in evaluate_candidates(candidates(context), context, store(), now=SEED_NOW)}

    assert by_action[A.SEND_CUSTOMER_REMINDER].eligible
    assert codes(by_action[A.DRAFT_MESSAGE]) == [R.CONVERSATION_CLOSED]


def test_new_conversation_is_not_blocked_by_a_previous_closed_one() -> None:
    # api-call-examples 2.6 suppresses the ended conversation_id, not the merchant:
    # a tick that opens a new conversation passes no conversation.
    assert evaluate(pick(ctx(COMPLIANCE), A.SEND_ALERT), ctx(COMPLIANCE)).eligible


@pytest.mark.parametrize(
    "turns, blocked",
    [
        ((("vera", "a"), ("vera", "b")), False),
        ((("vera", "a"), ("vera", "b"), ("vera", "c")), True),
        ((("vera", "a"), ("vera", "b"), ("vera", "c"), ("vera", "d")), True),
        ((("vera", "a"), ("vera", "b"), ("vera", "c"), ("merchant", "ok")), False),
        ((("vera", "a"), ("merchant", "ok"), ("vera", "b"), ("vera", "c")), False),
    ],
)
def test_nudge_limit_counts_consecutive_unanswered_messages(turns: tuple, blocked: bool) -> None:
    # m_001's history ends with a merchant reply, so only the live turns count.
    result = _merchant_candidate(conversation(M_MEERA, state="waiting", turns=turns))

    assert (R.NUDGE_LIMIT_REACHED in codes(result)) is blocked


def test_nudge_limit_includes_unanswered_history() -> None:
    # m_002's history ends with one unanswered Vera message (merchant_no_reply).
    two_more = conversation(M_BHARAT, state="waiting", turns=(("vera", "a"), ("vera", "b")))
    one_more = conversation(M_BHARAT, state="waiting", turns=(("vera", "a"),))

    assert R.NUDGE_LIMIT_REACHED in codes(_merchant_candidate(two_more, PERF_DIP, A.SEND_ALERT))
    assert _merchant_candidate(one_more, PERF_DIP, A.SEND_ALERT).eligible


def test_nudge_limit_detail_and_constant() -> None:
    turns = tuple(("vera", str(i)) for i in range(UNANSWERED_NUDGE_LIMIT))
    result = _merchant_candidate(conversation(M_MEERA, state="waiting", turns=turns))

    assert UNANSWERED_NUDGE_LIMIT == 3
    assert next(r.detail for r in result.reasons if r.code is R.NUDGE_LIMIT_REACHED) == "unanswered=3; limit=3"


def test_nudge_limit_for_customer_thread_counts_only_that_thread() -> None:
    unanswered = tuple(("vera", str(i)) for i in range(3))
    context = ctx(RECALL, conversation=conversation(M_MEERA, C_PRIYA, state="waiting", turns=unanswered))
    by_action = {r.candidate.action: r for r in evaluate_candidates(candidates(context), context, store(), now=SEED_NOW)}

    assert R.NUDGE_LIMIT_REACHED in by_action[A.SEND_CUSTOMER_REMINDER].reason_codes
    assert by_action[A.DRAFT_MESSAGE].eligible

    replied = conversation(M_MEERA, C_PRIYA, state="waiting", turns=(*unanswered, ("customer", "Yes please")))
    assert evaluate(pick(ctx(RECALL, conversation=replied), A.SEND_CUSTOMER_REMINDER), ctx(RECALL, conversation=replied)).eligible


# --------------------------------------------------------------------------- #
# Suppression (read-only; key semantics unchanged)
# --------------------------------------------------------------------------- #


def test_unsuppressed_candidate_is_eligible() -> None:
    assert _all_eligible(DIGEST, store())


def _all_eligible(trigger_id: str, suppression: SuppressionStore, now: datetime = SEED_NOW) -> bool:
    context = ctx(trigger_id)
    return all(r.eligible for r in evaluate_candidates(candidates(context), context, suppression, now=now))


def test_suppressed_key_rejects_every_candidate_that_shares_it() -> None:
    context = ctx(DIGEST)
    s = store()
    s.suppress(context.suppression_key)

    results = evaluate_candidates(candidates(context), context, s, now=SEED_NOW)

    assert len(results) >= 2
    assert all(r.reason_codes == (R.SUPPRESSED,) for r in results)
    assert all(r.reasons[0].detail == f"key={context.suppression_key}; expires_at=none" for r in results)


def test_suppression_uses_the_candidate_key_verbatim() -> None:
    context = ctx(RECALL)
    reminder = pick(context, A.SEND_CUSTOMER_REMINDER)
    assert reminder.suppression_key == context.trigger["suppression_key"] == "recall:c_001_priya_for_m001:6mo"
    s = store()
    s.suppress("recall:c_001_priya_for_m001")  # a prefix is a different key

    assert evaluate(reminder, context, s).eligible
    s.suppress("recall:c_001_priya_for_m001:6mo")
    assert codes(evaluate(reminder, context, s)) == [R.SUPPRESSED]


def test_independent_keys_do_not_interfere() -> None:
    s = store()
    s.suppress(ctx(DIGEST).suppression_key)

    assert not _all_eligible(DIGEST, s)
    assert _all_eligible(COMPLIANCE, s)  # same merchant, different trigger key


def test_customer_scoped_key_does_not_block_the_merchants_other_triggers() -> None:
    s = store()
    s.suppress(ctx(RECALL).suppression_key)

    assert not _all_eligible(RECALL, s)
    assert _all_eligible(DIGEST, s)


def test_merchant_suppression_blocks_all_of_that_merchants_triggers_only() -> None:
    s = store()
    s.suppress(merchant_suppression_key(M_MEERA))

    for trigger_id in (DIGEST, COMPLIANCE, RECALL):
        context = ctx(trigger_id)
        for r in evaluate_candidates(candidates(context), context, s, now=SEED_NOW):
            assert r.reason_codes == (R.MERCHANT_SUPPRESSED,)
    assert _all_eligible(PERF_DIP, s)  # a different merchant


def test_merchant_suppression_key_is_namespaced() -> None:
    assert merchant_suppression_key(M_MEERA) == "suppress:merchant:m_001_drmeera_dentist_delhi"


@pytest.mark.parametrize(
    "offset, suppressed",
    [(timedelta(hours=1), True), (timedelta(0), False), (-timedelta(hours=1), False)],
    ids=["active", "at-expiry", "expired"],
)
def test_suppression_record_expiry_is_honoured(offset: timedelta, suppressed: bool) -> None:
    s = store()
    s.suppress(ctx(DIGEST).suppression_key, expires_at=SEED_NOW + offset)

    assert _all_eligible(DIGEST, s) is not suppressed


def test_no_action_ignores_suppression() -> None:
    context = ctx(SEASONAL)
    s = store()
    s.suppress(context.suppression_key)
    s.suppress(merchant_suppression_key(context.merchant_id))

    assert evaluate(pick(context, A.NO_ACTION), context, s).eligible


class ReadOnlySpy:
    """Exposes only ``peek``; any write attempt would raise AttributeError."""

    def __init__(self, inner: SuppressionStore) -> None:
        self.inner = inner
        self.calls: list[tuple[str, datetime]] = []

    def peek(self, key: str, now: datetime) -> SuppressionRecord | None:
        self.calls.append((key, now))
        return self.inner.peek(key, now)


def test_evaluation_only_reads_suppression() -> None:
    context = ctx(DIGEST)
    spy = ReadOnlySpy(store())

    results = evaluate_candidates(candidates(context), context, spy, now=SEED_NOW)  # type: ignore[arg-type]

    keys = {key for key, _ in spy.calls}
    assert keys == {context.suppression_key, merchant_suppression_key(M_MEERA)}
    assert {now for _, now in spy.calls} == {SEED_NOW}
    assert all(r.eligible for r in results)


def test_evaluation_leaves_the_store_untouched_including_expired_records() -> None:
    context = ctx(DIGEST)
    s = store()
    s.suppress("some:other:key", expires_at=SEED_NOW - timedelta(days=1))  # already expired
    s.suppress(context.suppression_key, expires_at=SEED_NOW + timedelta(days=1))
    before = {k: s.peek(k, SEED_NOW - timedelta(days=2)) for k in ("some:other:key", context.suppression_key)}

    for _ in range(3):
        evaluate_candidates(candidates(context), context, s, now=SEED_NOW + timedelta(days=2))

    assert len(s) == 2
    assert {k: s.peek(k, SEED_NOW - timedelta(days=2)) for k in before} == before


def test_repeated_evaluation_is_deterministic() -> None:
    context = ctx(RECALL)
    s = store()
    s.suppress(merchant_suppression_key(M_MEERA))
    batch = candidates(context)

    first = evaluate_candidates(batch, context, s, now=SEED_NOW)
    second = evaluate_candidates(batch, context, s, now=SEED_NOW)

    assert first == second


def test_repeated_identical_candidates_are_each_evaluated() -> None:
    context = ctx(DIGEST)
    candidate = pick(context, A.SEND_INSIGHT)

    results = evaluate_candidates([candidate, candidate], context, store(), now=SEED_NOW)

    assert len(results) == 2 and results[0] == results[1]


def test_peek_does_not_drop_expired_records_but_get_does() -> None:
    s = store()
    s.suppress("k", expires_at=SEED_NOW)

    assert s.peek("k", SEED_NOW) is None
    assert s.peek("k", SEED_NOW - timedelta(seconds=1)) is not None
    assert len(s) == 1
    assert s.get("k", SEED_NOW) is None
    assert len(s) == 0


# --------------------------------------------------------------------------- #
# Reason ordering, batch helpers, boundaries
# --------------------------------------------------------------------------- #


def test_all_failures_are_collected_in_declaration_order_without_duplicates() -> None:
    reminder = pick(ctx(RECALL), A.SEND_CUSTOMER_REMINDER)
    fake = Evidence(source=EvidenceSource.MERCHANT, field="performance.views", value=-1, formatted="views: -1", importance=0.5)
    padded = reminder.model_copy(update={"evidence": (*reminder.evidence, fake, fake)})
    p = parts(RECALL)
    p["merchant"]["offers"][_offer_index(p, reminder.selected_offer_id)]["status"] = "expired"
    context = build(p, conversation=conversation(M_MEERA, C_PRIYA, state="ended"))
    s = store()
    s.suppress(reminder.suppression_key)
    s.suppress(merchant_suppression_key(M_MEERA))

    result = evaluate(padded, context, s, now=context.expires_at + timedelta(days=1))

    assert codes(result) == [
        R.EVIDENCE_NOT_GROUNDED,
        R.TRIGGER_EXPIRED,
        R.OFFER_UNAVAILABLE,
        R.CONVERSATION_CLOSED,
        R.SUPPRESSED,
        R.MERCHANT_SUPPRESSED,
    ]
    ungrounded = result.reasons[0].detail.split(",")
    assert len(ungrounded) == len(set(ungrounded))


def test_batch_preserves_order_and_filters_without_ranking() -> None:
    context = ctx(FESTIVAL)
    batch = list(reversed(candidates(context)))
    s = store()

    results = evaluate_candidates(batch, context, s, now=SEED_NOW)

    assert [r.candidate for r in results] == batch
    assert list(eligible_candidates(results)) == batch


def test_eligible_candidates_drops_only_ineligible_results() -> None:
    context = ctx(DIGEST)
    batch = candidates(context)
    s = store()
    s.suppress(context.suppression_key)
    mixed = [*evaluate_candidates(batch, context, s, now=SEED_NOW), *evaluate_candidates(batch, context, store(), now=SEED_NOW)]

    assert list(eligible_candidates(mixed)) == batch


def test_eligibility_does_not_score_rank_or_select() -> None:
    source = Path(eligibility_module.__file__).read_text()
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert not {"score_candidate", "rank_candidates", "candidate_sort_key"} & imported
    assert "app.engine.scoring" not in source
    assert not any(hasattr(eligibility_module, name) for name in ("best_candidate", "select_winner", "select_plan"))


def test_eligibility_imports_without_fastapi() -> None:
    code = "import sys, app.engine.eligibility; assert 'fastapi' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).resolve().parent.parent)


# --------------------------------------------------------------------------- #
# Replay: tick -> candidate -> hypothetical action -> state -> next evaluation
#
# The "commit" and "reply handling" steps below are test fixtures standing in
# for later phases; eligibility itself never writes.
# --------------------------------------------------------------------------- #


def _commit_send(s: SuppressionStore, candidate: DecisionCandidate) -> None:
    s.suppress(candidate.suppression_key, reason="sent")


def test_replay_sent_trigger_is_not_resent_on_the_next_tick() -> None:
    s = store()
    tick1 = ctx(DIGEST)
    sent = pick(tick1, A.SEND_INSIGHT)
    assert evaluate(sent, tick1, s).eligible

    _commit_send(s, sent)
    tick2 = ctx(DIGEST)
    next_results = evaluate_candidates(candidates(tick2), tick2, s, now=SEED_NOW + timedelta(minutes=5))

    assert all(r.reason_codes == (R.SUPPRESSED,) for r in next_results)
    assert _all_eligible(COMPLIANCE, s, SEED_NOW + timedelta(minutes=5))


def test_replay_rejection_closes_the_conversation_but_not_the_merchant() -> None:
    s = store()
    tick1 = ctx(DIGEST)
    _commit_send(s, pick(tick1, A.SEND_INSIGHT))
    ended = conversation(M_MEERA, state="ended", turns=(("vera", "digest"), ("merchant", "Not interested. Stop messaging me.")))

    in_thread = ctx(COMPLIANCE, conversation=ended)
    assert codes(evaluate(pick(in_thread, A.SEND_ALERT), in_thread, s)) == [R.CONVERSATION_CLOSED]
    assert evaluate(pick(ctx(COMPLIANCE), A.SEND_ALERT), ctx(COMPLIANCE), s).eligible  # new conversation


def test_replay_hostile_merchant_suppression_until_it_expires() -> None:
    s = store()
    until = SEED_NOW + timedelta(days=30)  # the value comes from the writer (api-call-examples 4.3), not eligibility
    s.suppress(merchant_suppression_key(M_MEERA), reason="hostile", expires_at=until)

    assert not _all_eligible(COMPLIANCE, s, SEED_NOW + timedelta(days=1))
    assert _all_eligible(COMPLIANCE, s, until)


def test_replay_mid_test_context_update_invalidates_stale_candidates() -> None:
    old_ctx = ctx(DIGEST)
    stale = pick(old_ctx, A.SEND_INSIGHT)
    fact = cited_path(stale, EvidenceSource.MERCHANT)
    p = parts(DIGEST)
    set_path(p["merchant"], fact.field, "updated")
    new_ctx = build(p)

    assert R.EVIDENCE_NOT_GROUNDED in codes(evaluate(stale, new_ctx))
    assert all(r.eligible for r in evaluate_candidates(candidates(new_ctx), new_ctx, store(), now=SEED_NOW))


def test_replay_unanswered_nudges_then_reply() -> None:
    turns: list[tuple[str, str]] = []
    for attempt in range(UNANSWERED_NUDGE_LIMIT):
        result = _merchant_candidate(conversation(M_MEERA, state="waiting", turns=tuple(turns)))
        assert result.eligible, attempt
        turns.append(("vera", f"nudge {attempt}"))

    assert R.NUDGE_LIMIT_REACHED in codes(_merchant_candidate(conversation(M_MEERA, state="waiting", turns=tuple(turns))))
    turns.append(("merchant", "ok, tell me"))
    assert _merchant_candidate(conversation(M_MEERA, state="qualifying", turns=tuple(turns))).eligible


def test_replay_expired_trigger_on_a_later_tick() -> None:
    s = store()
    tick = ctx(IPL)
    assert _all_eligible(IPL, s, SEED_NOW)
    later = _expiry(IPL) + timedelta(hours=1)

    results = evaluate_candidates(candidates(tick), tick, s, now=later)

    assert all(R.TRIGGER_EXPIRED in r.reason_codes for r in results)
    assert len(s) == 0
