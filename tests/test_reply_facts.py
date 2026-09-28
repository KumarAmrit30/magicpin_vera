"""Phase 3C: replies answer what was asked, from grounded facts only; Phase 2F decisions unchanged."""

import json
import re
from datetime import timedelta

import pytest

from app.engine.reply import ReplyIntent, classify_text
from app.engine.reply_composer import ReplyContext, Voice, compose_reply
from app.engine.reply_facts import (
    MAX_REQUESTS,
    REQUESTS,
    Fact,
    FactKind,
    opener_facts,
    requests_in,
    select,
)
from app.models.enums import ContextScope, CtaType, ReplyAction, TurnRole
from app.models.schemas import EndReply, SendReply, WaitReply
from tests.conftest import requires_dataset
from tests.test_planner import push, seeded
from tests.test_reply_composer import converse, judge_reply, outcome, without_composition
from tests.test_reply_dataset import REPLY_AT

pytestmark = requires_dataset

K = FactKind
RECALL = "trg_003_recall_due_priya"
DIP = "trg_004_perf_dip_bharat"
DIGEST = "trg_001_research_digest_dentists"
SUPPLY = "trg_018_supply_atorvastatin_recall"
REFILL = "trg_019_chronic_refill_grandfather"
TRIAL = "trg_017_kids_yoga_trial_followup_karthik"
WEBINAR = "trg_022_cde_webinar_dentists"
INTERNALS = ("[uncomposed", "trg_", "plan_", "suppress", "score", "priority", "confidence", "rationale", "Vera", "{}")


def body(trigger_id: str, *messages: str) -> str:
    *_, decisions = converse(trigger_id, list(messages))
    return decisions[-1].response.body


def numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


# --------------------------------------------------------------------------- #
# Question -> requested fact kinds (explicit table)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("message", "names"), [
    ("How much is the cleaning?", ["price"]),
    ("What does it cost?", ["price"]),
    ("Haircut kitne ka hai?", ["price"]),
    ("Do you have any evening slots?", ["availability"]),
    ("What times are available?", ["availability"]),
    ("When can I come?", ["availability"]),
    ("When is my cleaning due?", ["due"]),
    ("When is my appointment?", ["appointment"]),
    ("When did I last come in?", ["last_visit"]),
    ("How many customers are affected?", ["count"]),
    ("Where is this research from?", ["source"]),
    ("What's the deadline?", ["deadline"]),
    ("When is it?", ["date"]),
    ("How much is it and when can I come?", ["price", "availability"]),
    ("Why did you flag this?", []),
    ("Tell me more.", []),
    ("Can you recommend a good movie?", []),
])
def test_requests_are_read_from_explicit_phrases(message: str, names: list[str]) -> None:
    assert [r.name for r in requests_in(message)] == names


def test_request_table_is_small_and_bounded() -> None:
    assert len({r.name for r in REQUESTS}) == len(REQUESTS)
    assert len(requests_in("How much, when, how many, which source, what deadline and any slots?")) == MAX_REQUESTS


# --------------------------------------------------------------------------- #
# Test matrix (customer conversation: Priya's recall)
# --------------------------------------------------------------------------- #


def test_price_question_gets_the_price() -> None:
    answer = body(RECALL, "How much is the cleaning?")
    assert answer.startswith("Our current offer is Dental Cleaning @ \u20b9299.")
    assert "12 Nov" not in answer


def test_availability_question_gets_the_slots_in_the_asked_part_of_day() -> None:
    assert body(RECALL, "Do you have any evening slots?") == (
        "Yes \u2014 we have Wed 5 Nov, 6pm or Thu 6 Nov, 5pm open. Which slot works best for you?"
    )


def test_availability_outside_the_asked_part_of_day_says_so() -> None:
    assert body(RECALL, "Is there a morning slot?").startswith(
        "We don't have a morning slot open. The open slots are Wed 5 Nov, 6pm or Thu 6 Nov, 5pm."
    )
    assert body(TRIAL, "Any evening slots?").startswith("We don't have an evening slot open. The open slots are Sat 3 May, 8am.")


def test_due_date_question_gets_the_due_date() -> None:
    assert body(RECALL, "When is my cleaning due?").startswith("Aapke liye 6 month cleaning 12 Nov 2026 ko due hai.")


def test_appointment_question_without_a_booking_is_honest_and_offers_slots() -> None:
    answer = body(RECALL, "When is my appointment?")
    assert answer.startswith("We don't have a booked appointment on file. We have Wed 5 Nov, 6pm or Thu 6 Nov, 5pm open.")


def test_appointment_question_with_a_booking_gets_its_time() -> None:
    state = seeded()
    push(state, "trigger", "trg_appt", {
        "id": "trg_appt", "kind": "appointment_tomorrow", "scope": "customer", "merchant_id": "m_001_drmeera_dentist_delhi",
        "customer_id": "c_001_priya_for_m001", "payload": {"appointment_iso": "2026-04-27T18:00:00+05:30", "service": "cleaning"},
    })
    state.conversation_store.create("conv_appt", merchant_id="m_001_drmeera_dentist_delhi", customer_id="c_001_priya_for_m001",
                                    trigger_id="trg_appt")
    state.conversation_store.append_message("conv_appt", role=TurnRole.VERA, body="Hi Priya, Dr. Meera's Dental Clinic here. "
                                            "Reminder for tomorrow. Reply CONFIRM to keep it.", sent_at=REPLY_AT)
    decision = judge_reply(state, "When is my appointment?", "conv_appt", customer_id="c_001_priya_for_m001", from_role="customer")
    assert decision.response.body.startswith("Your appointment is on 27 Apr 2026, 6pm.")


def test_multi_fact_question_answers_both_with_one_cta() -> None:
    answer = body(RECALL, "How much is it and when can I come?")
    assert answer == (
        "Our current offer is Dental Cleaning @ \u20b9299. We have Wed 5 Nov, 6pm or Thu 6 Nov, 5pm open. "
        "Which slot works best for you?"
    )
    assert answer.count("?") == 1 and "12 Nov" not in answer and "12 May" not in answer


def test_last_visit_question() -> None:
    assert body(RECALL, "When did I last come in?").startswith("Last service 12 May 2026 ko hui thi.")


def test_price_of_an_item_the_business_has_no_offer_for_is_not_guessed() -> None:
    answer = body("trg_007_bridal_followup_kavya", "How much is the cleaning?")
    assert answer.startswith("We don't have the price on file.") and "\u20b9" not in answer
    assert body("trg_007_bridal_followup_kavya", "How much is a haircut?").startswith("Our current offer is Haircut @ \u20b999.")


# --------------------------------------------------------------------------- #
# Merchant conversations
# --------------------------------------------------------------------------- #


def test_why_question_explains_the_grounded_reason() -> None:
    assert body(DIP, "Why did you flag this?").startswith("Here's why I raised it: Your calls are down 50%.")


def test_count_question_gets_the_count() -> None:
    assert body(DIGEST, "How many patients are affected?").startswith("You have 124 matching patients on record.")


def test_count_question_without_a_count_is_not_answered_with_a_guess() -> None:
    answer = body(SUPPLY, "How many customers are affected?")
    assert answer.startswith("I don't have that number on record. You have 240 chronic-Rx customers on record.")
    assert numbers(answer) == {"240"}


def test_a_count_answers_only_what_it_counts() -> None:
    assert body(SUPPLY, "How many chronic-Rx customers do I have?").startswith("You have 240 chronic-Rx customers on record.")
    assert body(DIGEST, "How many patients visited last month?").startswith(
        "I don't have that number on record. You have 124 matching patients on record.")


def test_source_and_event_questions() -> None:
    assert body(DIGEST, "Where is this research from?").startswith("Source: JIDA Oct 2026, p.14.")
    assert body(WEBINAR, "When is it?").startswith("Date: 2 May 2026, 7pm.")
    assert body(WEBINAR, "How much is the fee?").startswith("Fee: free for members.")


def test_merchant_price_question_never_borrows_the_merchants_own_offer() -> None:
    answer = body(DIGEST, "How much does it cost?")
    assert answer.startswith("I don't have the price on record.")
    assert "\u20b9" not in answer


def test_thresholds_are_not_prices() -> None:
    answer = body(REFILL, "How much will it cost?")
    assert answer.startswith("We don't have the price on file.") and "\u20b9" not in answer


def test_opener_wording_is_preferred_for_plain_facts() -> None:
    assert body(REFILL, "When does it run out?").startswith(
        "Aapki medicines (metformin, atorvastatin and telmisartan) 28 Apr 2026 ko khatam hongi."
    )


# --------------------------------------------------------------------------- #
# Ambiguity
# --------------------------------------------------------------------------- #


def test_tell_me_more_stays_a_conservative_check_in() -> None:
    """"Tell me more." is a Phase 2F ``unclear``: one binary question about the pending offer, no new facts."""
    *_, (decision,) = converse(DIGEST, ["Tell me more."])
    assert decision.reading.intent is ReplyIntent.UNCLEAR
    assert decision.response.body == "Just to check \u2014 want me to draft a short explainer you can share with your patients?"


def test_general_questions_move_to_the_next_unshared_fact() -> None:
    *_, (first, second) = converse(DIGEST, ["Can you explain?", "Can you explain more?"])
    assert "What I have on record: New research: 3-month fluoride varnish" in first.response.body
    assert "What I have on record: Suggested step: Reassess recall interval" in second.response.body


def test_question_with_no_context_at_all_invents_nothing() -> None:
    decision = judge_reply(seeded(), "How much is it?", "conv_blank", merchant_id="m_404")
    assert decision.response.body == "I don't have the price on record. What would you like to do next?"


# --------------------------------------------------------------------------- #
# Voice and leakage
# --------------------------------------------------------------------------- #


CUSTOMER_QUESTIONS = ["How much is the cleaning?", "Do you have any evening slots?", "When is my appointment?",
                      "How much is it and when can I come?", "When did I last come in?", "When is it due?"]


@pytest.mark.parametrize("trigger_id", [RECALL, TRIAL, REFILL, "trg_007_bridal_followup_kavya", "trg_015_winback_rashmi"])
@pytest.mark.parametrize("message", CUSTOMER_QUESTIONS)
def test_customer_answers_speak_as_the_business_and_leak_nothing(trigger_id: str, message: str) -> None:
    state, action, (decision,) = converse(trigger_id, [message])
    answer = decision.response.body
    assert not [w for w in INTERNALS if w in answer], answer
    assert "I can" not in answer and "I'll" not in answer and "I don't" not in answer
    assert answer.count("?") == 1 and answer.endswith("?")
    contexts = [state.context_store.get(ContextScope(scope), context_id).payload for scope, context_id in (
        ("trigger", trigger_id), ("merchant", action.merchant_id), ("customer", action.customer_id))]
    assert numbers(answer) <= numbers(action.body) | numbers(json.dumps(contexts, ensure_ascii=False)), (answer, action.body)


def test_judge_opened_customer_price_question_greets_as_the_business() -> None:
    decision = judge_reply(seeded(), "How much is it?", "conv_c", customer_id="c_001_priya_for_m001", from_role="customer")
    assert decision.response.body == (
        "Namaste Priya, Dr. Meera's Dental Clinic here. Our current offer is Dental Cleaning @ \u20b9299. What works best for you?"
    )


def test_merchant_answers_speak_as_vera() -> None:
    answer = body(DIGEST, "How many patients are affected?")
    assert "I can draft" in answer and "We can" not in answer


# --------------------------------------------------------------------------- #
# Conversational context
# --------------------------------------------------------------------------- #


def test_latest_vera_message_wins_between_same_kind_facts() -> None:
    older = Fact(K.PRICE, "Current offer: Haircut @ \u20b999.", "merchant:offers[0].title", ("Haircut @ \u20b999",))
    newer = Fact(K.PRICE, "Current offer: Hair Spa @ \u20b9499.", "merchant:offers[1].title", ("Hair Spa @ \u20b9499",))
    turns = ["Hi — Current offer: Haircut @ \u20b999.", "We also have Hair Spa @ \u20b9499 this week."]
    assert select("How much is it?", [older, newer], turns).facts == (newer,)
    assert select("How much is a haircut?", [older, newer], turns).facts == (older,)


def test_requested_kind_beats_the_first_fact_of_the_previous_message() -> None:
    pool = [Fact(K.DUE_DATE, "Your cleaning is due on 12 Nov.", "turn:0"), Fact(K.PRICE, "Fee: \u20b9299.", "turn:0")]
    assert select("How much is it?", pool, ["Your cleaning is due on 12 Nov. Fee: \u20b9299."]).facts == (pool[1],)


def test_continuity_why_then_commit_refers_to_the_action_discussed() -> None:
    *_, (why, commit) = converse(DIP, ["Why?", "Okay, let's do it"])
    assert why.response.body.startswith("Here's why I raised it: Your calls are down 50%.")
    assert commit.response.body == "Great. Reply CONFIRM and I'll walk you through it, or STOP to end here."


def test_question_then_yes_confirms_the_offered_slot() -> None:
    *_, (question, yes) = converse(RECALL, ["How much is the cleaning?", "yes"])
    assert question.response.body.startswith("Our current offer is Dental Cleaning @ \u20b9299.")
    assert yes.response.body == "Great. Reply CONFIRM and we'll book Wed 5 Nov, 6pm, or STOP to end here."


def test_opener_facts_are_tagged_by_kind() -> None:
    state, action, _ = converse(RECALL, [])
    facts = opener_facts(state.conversation_store.get(action.conversation_id))
    assert {f.kind: f.sentence for f in facts} == {
        K.DUE_DATE: "Aapke liye 6 month cleaning 12 Nov 2026 ko due hai.",
        K.LAST_VISIT: "Last service 12 May 2026 ko hui thi.",
        K.PRICE: "Abhi Dental Cleaning @ \u20b9299 offer chal raha hai.",
        K.AVAILABILITY: "Aapke liye slots ready hain: Wed 5 Nov, 6pm or Thu 6 Nov, 5pm.",
    }


# --------------------------------------------------------------------------- #
# Phase 2F isolation, determinism, templates
# --------------------------------------------------------------------------- #

QUESTIONS_3C = [*CUSTOMER_QUESTIONS, "Why did you flag this?", "How many patients are affected?", "Where is this from?",
                "Can you explain?", "Tell me more.", "Cancel", "CONFIRM", "STOP"]


@pytest.mark.parametrize("trigger_id", [RECALL, DIP, DIGEST, REFILL])
@pytest.mark.parametrize("message", QUESTIONS_3C)
def test_phase_2f_decision_is_unchanged_by_phase_3c(trigger_id: str, message: str, monkeypatch: pytest.MonkeyPatch) -> None:
    state, action, (after,) = converse(trigger_id, [message])
    composed = outcome(state, action, after)
    without_composition(monkeypatch)
    state, action, (before,) = converse(trigger_id, [message])
    assert outcome(state, action, before) == composed


def test_answers_are_deterministic_and_time_independent() -> None:
    from tests.test_reply_dataset import answer, opened

    bodies = set()
    for shift in (timedelta(0), timedelta(days=90)):
        state, action = opened(RECALL)
        bodies.add(answer(state, action, "How much is it and when can I come?", received_at=REPLY_AT + shift).response.body)
    assert len(bodies) == 1


def test_answer_template_is_consistent() -> None:
    pool = [Fact(K.PRICE, "Current offer: Haircut @ \u20b999.", "merchant:offers[0].title", ("Haircut @ \u20b999",))]
    context = ReplyContext(voice=Voice.BUSINESS, offer="book Sat 3 May, 8am", offer_source="turn:0",
                           answer=select("How much?", pool, []))
    composed = compose_reply("question", CtaType.OPEN_ENDED, context)
    assert composed.template_name == "vera_reply_answer_v1"
    assert composed.template == "Our current offer is {{1}}. We can {{2}}. What works best for you?"
    assert "merchant:offers[0].title" in composed.sources


def test_repeated_price_questions_never_repeat_a_body() -> None:
    """Differently worded: identical messages repeated are Phase 2F auto-replies."""
    *_, decisions = converse(RECALL, ["How much is it?", "How much does it cost?", "What's the price?", "How much?"])
    bodies = [d.response.body for d in decisions]
    assert len(set(bodies)) == 4
    assert all("Dental Cleaning @ \u20b9299" in b for b in bodies)


# --------------------------------------------------------------------------- #
# CONFIRM / STOP: advertised keywords are the ones Phase 2F honours
# --------------------------------------------------------------------------- #


def test_advertised_confirm_and_stop_words_mean_what_the_reply_says() -> None:
    assert classify_text("CONFIRM")[0] is ReplyIntent.AFFIRMATIVE
    assert classify_text("STOP")[0] is ReplyIntent.OPT_OUT
    for voice in Voice:
        composed = compose_reply("affirmative", CtaType.BINARY_CONFIRM_CANCEL, ReplyContext(voice=voice, offer="book it"))
        assert "Reply CONFIRM" in composed.body and "or STOP to end here." in composed.body
        assert "CANCEL" not in composed.body


def test_stop_after_a_confirm_prompt_ends_and_confirm_proceeds() -> None:
    *_, (_, stop) = converse(DIP, ["Okay, let's do it", "STOP"])
    assert isinstance(stop.response, EndReply)
    *_, (_, confirm) = converse(DIP, ["Okay, let's do it", "CONFIRM"])
    assert isinstance(confirm.response, SendReply) and confirm.reading.intent is ReplyIntent.AFFIRMATIVE


def test_cancel_remains_unclear_in_phase_2f() -> None:
    """Observed limitation, kept on purpose: no challenge material defines CANCEL, so Phase 2F is unchanged."""
    assert classify_text("Cancel")[0] is ReplyIntent.UNCLEAR
    *_, (_, cancel) = converse(DIP, ["Okay, let's do it", "Cancel"])
    assert cancel.response.action is ReplyAction.SEND and cancel.response.cta is CtaType.BINARY_YES_NO
    assert not isinstance(cancel.response, WaitReply)
