"""Phase 3B reply composer: Phase 2F decisions worded, never changed.

Engine-level (no FastAPI). "Before 3B" runs are the same ``handle_reply`` with
the composition step disabled, so every comparison isolates wording from the
Phase 2F decision.
"""

import re
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

import app.engine.reply as reply_module
from app.engine.composer import reads_as
from app.engine.eligibility import merchant_suppression_key
from app.engine.reply import (
    AUTO_REPLY_WAIT_SECONDS,
    DEFER_WAIT_SECONDS,
    REPLY_PLACEHOLDER_PREFIX,
    ReplyDecision,
    ReplyIntent,
    handle_reply,
)
from app.engine.reply_composer import (
    CUSTOMER_TOPICS,
    MERCHANT_TOPICS,
    ReplyContext,
    Voice,
    compose_reply,
    opening_fact,
    pending_offer,
    reply_context,
    sentences,
)
from app.models.enums import ConversationState, CtaType, SendAs, TurnRole
from app.models.schemas import EndReply, ReplyRequest, SendReply, TickAction, WaitReply
from app.state.container import StateContainer
from tests.conftest import DATASET_DIR, load_seed_dataset, requires_dataset
from tests.test_planner import seeded, tick
from tests.test_reply_dataset import REPLY_AT, answer, opened

pytestmark = requires_dataset

R = ReplyIntent
S = ConversationState
DIGEST = "trg_001_research_digest_dentists"
RECALL = "trg_003_recall_due_priya"
CURIOUS = "trg_008_curious_ask_studio11"
REFILL = "trg_019_chronic_refill_grandfather"
WEBINAR = "trg_022_cde_webinar_dentists"
M001 = "m_001_drmeera_dentist_delhi"
C001 = "c_001_priya_for_m001"

REPRESENTATIVE: dict[str, str] = {
    "hostile": "Why are you bothering me. This is useless. Stop sending these.",
    "opt_out": "Stop messaging me please",
    "not_interested": "Not interested. Stop messaging me.",
    "auto_reply": "Thank you for contacting us! Our team will respond shortly.",
    "time_question": "What time will you call me tomorrow?",
    "objection": "This is too expensive for us",
    "affirmative": "Ok, let's do it. What's next?",
    "question": "How long does it take?",
    "off_topic": "Btw can you also help me with my GST filing this month?",
    "unclear": "hmm",
    "movie": "Can you recommend a good movie?",
}
SEED_TRIGGERS = [DIGEST, "trg_002_compliance_dci_radiograph", RECALL, CURIOUS, "trg_010_ipl_match_delhi", "trg_015_winback_rashmi", REFILL, WEBINAR]
INTERNALS = ("[uncomposed", "trigger", "trg_", "suppress", "score", "priority", "confidence", "plan_id", "rationale", "{{", "{}")


def emitted_seed_triggers() -> list[str]:
    """Seed triggers that open a conversation on their own tick."""
    if not DATASET_DIR.is_dir():
        return []
    return [t for t, _ in load_seed_dataset()["trigger"] if tick(seeded(), t).actions]


def without_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reply_module, "realize", lambda decision, conversation, contexts: decision)


def outcome(state: StateContainer, action: TickAction, decision: ReplyDecision) -> tuple[Any, ...]:
    """Everything Phase 2F decides, with the send body left out."""
    response = decision.response.model_dump(exclude={"body"})
    conversation = state.conversation_store.get(action.conversation_id)
    keys = (action.suppression_key, merchant_suppression_key(action.merchant_id))
    records = tuple((k, state.suppression_store.peek(k, REPLY_AT) is not None) for k in keys)
    return (
        response, decision.state, decision.reading, decision.suppression, conversation.state,
        [(t.role, t.turn_number) for t in conversation.turns], len(state.suppression_store), records,
    )


def converse(trigger_id: str, messages: list[str]) -> tuple[StateContainer, TickAction, list[ReplyDecision]]:
    state, action = opened(trigger_id)
    return state, action, [answer(state, action, m, turn=t) for t, m in enumerate(messages, start=2)]


def judge_reply(state: StateContainer, message: str, conversation_id: str, turn: int = 2, **fields: Any) -> ReplyDecision:
    request = {
        "conversation_id": conversation_id, "merchant_id": M001, "customer_id": None, "from_role": "merchant",
        "message": message, "received_at": REPLY_AT + timedelta(minutes=turn), "turn_number": turn,
    }
    return handle_reply(state, ReplyRequest(**(request | fields)))


# --------------------------------------------------------------------------- #
# Classification isolation: the Phase 2F decision is identical before and after 3B
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("trigger_id", SEED_TRIGGERS)
@pytest.mark.parametrize("label", sorted(REPRESENTATIVE))
def test_phase_2f_decision_is_identical_with_and_without_composition(
    trigger_id: str, label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = REPRESENTATIVE[label]
    state, action, (after,) = converse(trigger_id, [message])
    composed = outcome(state, action, after)

    without_composition(monkeypatch)
    state, action, (before,) = converse(trigger_id, [message])

    assert outcome(state, action, before) == composed
    if isinstance(before.response, SendReply):
        assert before.response.body.startswith(REPLY_PLACEHOLDER_PREFIX)
        assert REPLY_PLACEHOLDER_PREFIX not in after.response.body
    else:
        assert after.response == before.response


def test_multi_turn_sequences_keep_every_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    script = [REPRESENTATIVE["question"], REPRESENTATIVE["objection"], REPRESENTATIVE["time_question"], "yes", "yes", "ok go ahead"]
    state, action, after = converse(DIGEST, script)
    composed = [outcome(state, action, d) for d in after]

    without_composition(monkeypatch)
    state, action, before = converse(DIGEST, script)
    assert [outcome(state, action, d) for d in before] == composed


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("trigger_id", emitted_seed_triggers())
def test_every_send_is_composed_and_every_wait_end_keeps_its_contract(trigger_id: str) -> None:
    for label, message in sorted(REPRESENTATIVE.items()):
        state, action, (decision,) = converse(trigger_id, [message])
        response = decision.response
        if isinstance(response, SendReply):
            assert response.body.strip() and REPLY_PLACEHOLDER_PREFIX not in response.body, label
            assert not [w for w in INTERNALS if w in response.body], (label, response.body)
            vera = [t for t in state.conversation_store.get(action.conversation_id).turns if t.role is TurnRole.VERA]
            assert vera[-1].body == response.body
        elif isinstance(response, WaitReply):
            assert response.wait_seconds == DEFER_WAIT_SECONDS, label
            assert set(response.model_dump()) == {"action", "wait_seconds", "rationale"}
        else:
            assert set(response.model_dump()) == {"action", "rationale"}


def test_auto_reply_sequence_waits_exactly_and_ends() -> None:
    """api-call-examples 4.1 on the CDE webinar invite: prompt, wait 24h, end, end."""
    auto = REPRESENTATIVE["auto_reply"]
    state, action, decisions = converse(WEBINAR, [auto, auto, auto, auto])

    first, second, third, fourth = (d.response for d in decisions)
    assert isinstance(first, SendReply) and first.cta is CtaType.BINARY_YES_NO
    assert first.body.startswith("Looks like an automated reply. When the owner sees this, want me to ")
    assert first.body.endswith("?") and first.body.count("?") == 1
    assert second == WaitReply(wait_seconds=AUTO_REPLY_WAIT_SECONDS, rationale=second.rationale)
    assert isinstance(third, EndReply) and isinstance(fourth, EndReply)
    assert state.conversation_store.get(action.conversation_id).state is S.ENDED


def test_reply_after_the_conversation_ended_stays_closed() -> None:
    state, action, (hostile, later) = converse(DIGEST, [REPRESENTATIVE["hostile"], "ok fine, what's next?"])

    assert isinstance(hostile.response, EndReply) and isinstance(later.response, EndReply)
    turns = state.conversation_store.get(action.conversation_id).turns
    assert [t.role for t in turns].count(TurnRole.VERA) == 1


# --------------------------------------------------------------------------- #
# Replay / case-study messages
# --------------------------------------------------------------------------- #


def test_lets_do_it_switches_to_action_with_an_explicit_next_step() -> None:
    state, action, (decision,) = converse(DIGEST, [REPRESENTATIVE["affirmative"]])
    body = decision.response.body

    assert decision.response.cta is CtaType.BINARY_CONFIRM_CANCEL
    assert body == "Great. Reply CONFIRM and I'll draft a short explainer you can share with your patients, or STOP to end here."
    assert not any(w in body.lower() for w in ["would you", "do you", "can you tell", "what if", "how about"])
    assert not re.search(r"\b(done|is live|sent|scheduled)\b", body.lower())


def test_time_question_waits_thirty_minutes_without_a_body() -> None:
    _, _, (decision,) = converse(DIGEST, [REPRESENTATIVE["time_question"]])
    assert decision.response == WaitReply(wait_seconds=DEFER_WAIT_SECONDS, rationale=decision.response.rationale)


def test_objection_acknowledges_and_restates_the_grounded_reason() -> None:
    _, _, (decision,) = converse(DIGEST, [REPRESENTATIVE["objection"]])
    body = decision.response.body

    assert body.startswith("Fair point. I raised it because of this: New research: 3-month fluoride varnish recall")
    assert "No pressure either way." in body
    assert body.endswith("What would make it worthwhile for you?") and body.count("?") == 1


def test_off_topic_declines_and_returns_to_the_trigger() -> None:
    _, _, (decision,) = converse(DIGEST, [REPRESENTATIVE["off_topic"]])
    body = decision.response.body

    assert body.startswith("That's outside my scope here. Coming back to the research update: I can draft ")
    assert decision.response.cta is CtaType.OPEN_ENDED and body.count("?") == 1
    assert "GST" not in body


def test_hard_no_and_hostility_end_without_a_body() -> None:
    for message in (REPRESENTATIVE["not_interested"], REPRESENTATIVE["hostile"]):
        _, _, (decision,) = converse(DIGEST, [message])
        assert isinstance(decision.response, EndReply)
        assert "body" not in decision.response.model_dump()


def test_why_question_answers_from_the_opening_fact() -> None:
    _, _, (decision,) = converse("trg_004_perf_dip_bharat", ["Why did you flag this?"])
    assert decision.response.body.startswith("Here's why I raised it: Your calls are down 50%.")


def test_question_classified_as_question_is_answered_conservatively() -> None:
    """"Can you recommend a good movie?" is a Phase 2F question; 3B only words it."""
    _, _, (decision,) = converse(DIGEST, [REPRESENTATIVE["movie"]])
    body = decision.response.body

    assert decision.reading.intent is R.QUESTION
    assert body.startswith("Good question. What I have on record: New research:")
    assert "movie" not in body.lower()


# --------------------------------------------------------------------------- #
# Grounding
# --------------------------------------------------------------------------- #


def numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


@pytest.mark.parametrize("trigger_id", emitted_seed_triggers())
def test_numbers_dates_and_offers_come_from_the_conversation(trigger_id: str) -> None:
    for label in ("question", "objection", "off_topic", "affirmative", "unclear", "auto_reply"):
        state, action, (decision,) = converse(trigger_id, [REPRESENTATIVE[label]])
        if not isinstance(decision.response, SendReply):
            continue
        assert numbers(decision.response.body) <= numbers(action.body), (label, decision.response.body)


def test_offer_and_dates_are_quoted_from_the_opening_message() -> None:
    state, action, decisions = converse(RECALL, [REPRESENTATIVE["objection"], "hmm ok?"])
    objection = decisions[0].response.body

    assert "Aapke liye 6 month cleaning 12 Nov 2026 ko due hai." in action.body
    assert "We reached out because of this: Aapke liye 6 month cleaning 12 Nov 2026 ko due hai." in objection
    assert "We can book Wed 5 Nov, 6pm." in objection


def test_missing_trigger_and_opener_leave_only_fixed_text() -> None:
    state = StateContainer.create()
    for turn, message in enumerate([REPRESENTATIVE["affirmative"], REPRESENTATIVE["question"], "hmm"], start=2):
        decision = judge_reply(state, message, "conv_unknown", turn=turn)
        body = decision.response.body
        assert not numbers(body), body
        assert "Meera" not in body and "Coming back to" not in body


def test_uncomposed_or_unusable_openers_are_never_quoted() -> None:
    state = seeded()
    state.conversation_store.create("conv_p", merchant_id=M001, trigger_id=DIGEST)
    state.conversation_store.append_message("conv_p", role=TurnRole.VERA, body="[uncomposed] opener | 42", sent_at=REPLY_AT)
    decision = judge_reply(state, REPRESENTATIVE["objection"], "conv_p")

    assert "42" not in decision.response.body and "[" not in decision.response.body
    assert decision.response.body == "Fair point. No pressure either way. What would make it worthwhile for you?"


def test_topic_is_only_named_for_known_trigger_kinds() -> None:
    assert set(MERCHANT_TOPICS) >= set(CUSTOMER_TOPICS)
    assert not [t for t in (*MERCHANT_TOPICS.values(), *CUSTOMER_TOPICS.values()) if re.search(r"\d|today|tomorrow", t)]


def test_extraction_helpers_reject_questions_and_markup() -> None:
    assert sentences("Dr. Meera — Your calls are down 50%. Want me to fix it?") == [
        "Dr. Meera — Your calls are down 50%.", "Want me to fix it?",
    ]
    state = seeded()
    state.conversation_store.create("conv_x", merchant_id=M001, trigger_id=DIGEST)
    state.conversation_store.append_message("conv_x", role=TurnRole.VERA, body="Dr. Meera — Is this {odd}? Want me to [x]?", sent_at=REPLY_AT)
    conversation = state.conversation_store.get("conv_x")
    assert opening_fact(conversation) is None
    assert pending_offer(conversation) is None


# --------------------------------------------------------------------------- #
# Voice
# --------------------------------------------------------------------------- #


def test_merchant_replies_speak_as_vera() -> None:
    _, _, (decision,) = converse(DIGEST, [REPRESENTATIVE["question"]])
    assert " I can " in f" {decision.response.body}" and "We can" not in decision.response.body


@pytest.mark.parametrize("trigger_id", [RECALL, REFILL, "trg_015_winback_rashmi", "trg_007_bridal_followup_kavya"])
def test_customer_replies_speak_as_the_business_and_leak_nothing(trigger_id: str) -> None:
    for label in ("question", "objection", "off_topic", "affirmative", "unclear"):
        state, action, (decision,) = converse(trigger_id, [REPRESENTATIVE[label]])
        assert action.send_as is SendAs.MERCHANT_ON_BEHALF
        body = decision.response.body
        assert "Vera" not in body and "I can" not in body and "I'll" not in body, body
        assert not [w for w in (*INTERNALS, "merchant", "customer state", "lapsed") if w in body], body


def test_first_reply_on_a_judge_opened_merchant_conversation_uses_the_salutation() -> None:
    decision = judge_reply(seeded(), REPRESENTATIVE["affirmative"], "conv_intent_1")
    assert decision.response.body == "Dr. Meera \u2014 Great. Reply CONFIRM and I'll take the next step, or STOP to end here."


def test_first_reply_on_a_judge_opened_customer_conversation_greets_as_the_business() -> None:
    decision = judge_reply(seeded(), "How much is it?", "conv_c", customer_id=C001, from_role="customer")
    body = decision.response.body
    assert body.startswith("Namaste Priya, Dr. Meera's Dental Clinic here. ")
    assert "Vera" not in body and body.count("?") == 1


def test_later_replies_do_not_reintroduce() -> None:
    state = seeded()
    judge_reply(state, "hmm", "conv_j", turn=2)
    second = judge_reply(state, "How long does it take?", "conv_j", turn=3)
    assert not second.response.body.startswith("Dr. Meera")


# --------------------------------------------------------------------------- #
# CTA
# --------------------------------------------------------------------------- #

CTX = ReplyContext(voice=Voice.VERA, topic="the research update", offer="draft a short explainer", offer_source="turn:0",
                   reason="New research: X.")


@pytest.mark.parametrize("intent", ["affirmative", "question", "objection", "off_topic", "unclear", "auto_reply"])
@pytest.mark.parametrize("cta", [CtaType.BINARY_YES_NO, CtaType.OPEN_ENDED, CtaType.BINARY_CONFIRM_CANCEL, CtaType.NONE])
@pytest.mark.parametrize("voice", list(Voice))
@pytest.mark.parametrize("grounded", [True, False])
def test_cta_shapes_hold_for_every_intent(intent: str, cta: CtaType, voice: Voice, grounded: bool) -> None:
    context = replace(CTX, voice=voice) if grounded else ReplyContext(voice=voice)
    composed = compose_reply(intent, cta, context)
    body = composed.body

    assert composed.cta is cta
    if cta in {CtaType.BINARY_YES_NO, CtaType.OPEN_ENDED}:
        assert body.count("?") == 1 and body.endswith("?"), body
        assert "CONFIRM" not in body
    elif cta is CtaType.BINARY_CONFIRM_CANCEL:
        assert "?" not in body and "Reply CONFIRM" in body and "or STOP to end here." in body, body
    else:
        assert "?" not in body and "CONFIRM" not in body, body


# --------------------------------------------------------------------------- #
# Templates and params
# --------------------------------------------------------------------------- #


def test_template_names_params_and_template_are_consistent() -> None:
    for intent in ("affirmative", "question", "objection", "off_topic", "unclear", "auto_reply"):
        composed = compose_reply(intent, CtaType.OPEN_ENDED, CTX)
        assert composed.template_name == f"vera_reply_{intent}_v1"
        assert reads_as(composed) == composed.body
        assert all(p in composed.body for p in composed.template_params)
    why = compose_reply("question", CtaType.OPEN_ENDED, ReplyContext(voice=Voice.VERA, reason="New research: X.", asks_why=True))
    assert why.template_name == "vera_reply_why_v1"


def test_params_never_carry_internal_ids() -> None:
    state, action, decisions = converse(DIGEST, [REPRESENTATIVE["question"]])
    conversation = state.conversation_store.get(action.conversation_id)
    composed = compose_reply("question", CtaType.OPEN_ENDED, reply_context(conversation, state.context_store))
    joined = " ".join(composed.template_params)
    assert action.trigger_id not in joined and action.suppression_key not in joined and "plan" not in joined


# --------------------------------------------------------------------------- #
# Determinism and uniqueness
# --------------------------------------------------------------------------- #


def test_composition_is_byte_identical_across_runs_and_times() -> None:
    bodies = []
    for shift in (timedelta(0), timedelta(days=400)):
        state, action = opened(DIGEST)
        decision = answer(state, action, REPRESENTATIVE["objection"], received_at=REPLY_AT + shift)
        bodies.append(decision.response.body)
    assert bodies[0] == bodies[1]
    assert compose_reply("question", CtaType.OPEN_ENDED, CTX) == compose_reply("question", CtaType.OPEN_ENDED, CTX)


def test_bodies_never_repeat_within_a_conversation() -> None:
    state, action, decisions = converse(DIGEST, ["yes", "yes", "yes", "yes", "yes"])
    bodies = [d.response.body for d in decisions]

    assert len(set(bodies)) == len(bodies) == 5
    assert all(b.endswith("or STOP to end here.") for b in bodies)
    vera = [t.body for t in state.conversation_store.get(action.conversation_id).turns if t.role is TurnRole.VERA]
    assert len(set(vera)) == len(vera)
