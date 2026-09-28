"""Phase 3D-B: customer-facing tick messages in Hinglish for customers who prefer Hindi.

Only the fact sentences of ``merchant_on_behalf`` messages change, and only when the
customer's ``identity.language_pref`` is ``hi`` or ``hi-en mix``. Greetings, CTAs,
values, template parameters, facts used and every decision field stay the same;
merchant messages and every other customer are byte-for-byte what they were.

The pre-change baseline (``fixtures/composer_language/pre_hinglish_compositions.json``)
is the composer's output for every eligible seed and expanded candidate before the change.
"""

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.engine.actions import ActionType, CTAType, DecisionScope
from app.engine.candidates import CandidateGenerationContext, generate_candidates
from app.engine.composer import CUSTOMER_PHRASES, HINDI_PREFS, compose, prefers_hindi
from app.engine.eligibility import evaluate_candidates
from app.engine.plans import DecisionPlan
from app.engine.reply_facts import SENTENCE_KINDS
from app.engine.selection import plan_from_candidate
from app.models.enums import SendAs
from app.state.suppression_store import SuppressionStore
from tests.conftest import SEED_NOW, expanded_context, load_seed_dataset, requires_dataset, seed_context, seed_context_parts
from tests.test_composer import all_candidate_plans, last_sentence, seed_plan, select
from tests.test_composer_optimizations import appointment_trigger
from tests.test_planner import push, seed_payload, seeded, tick
from tests.test_reply_dataset import answer

pytestmark = requires_dataset

BASELINE = json.loads((Path(__file__).parent / "fixtures" / "composer_language" / "pre_hinglish_compositions.json").read_text())
FIELDS = ("body", "template", "template_params", "facts_used", "cta", "send_as", "template_name")
INTERNALS = ("trg_", "plan_", "suppress", "score", "priority", "confidence", "rationale", "Vera", "{}", "{{", "_")
HINGLISH_WORDS = re.compile(r"\b(?:hai|hain|ko|kal|aapka|aapki|aapke|liye|thi|nahi|hongi)\b", re.IGNORECASE)

RECALL = "trg_003_recall_due_priya"          # T28: hi-en mix
REFILL = "trg_019_chronic_refill_grandfather"  # T07: hi
PRIYA, MEERA = "c_001_priya_for_m001", "m_001_drmeera_dentist_delhi"


def key(dataset: str, plan: DecisionPlan) -> str:
    return "|".join([dataset, plan.trigger_id, plan.action.value, plan.scope.value, plan.customer_id or "-", plan.selected_offer_id or "-"])


def row(message) -> dict[str, Any]:
    return {
        "body": message.body, "template": message.template, "template_params": list(message.template_params),
        "facts_used": list(message.facts_used), "cta": message.cta.value, "send_as": message.send_as.value,
        "template_name": message.template_name,
    }


def seed_candidate_plans() -> list[tuple[DecisionPlan, CandidateGenerationContext]]:
    out = []
    for trigger_id, _ in load_seed_dataset()["trigger"]:
        ctx = seed_context(trigger_id)
        for result in evaluate_candidates(generate_candidates(ctx), ctx, SuppressionStore(), now=SEED_NOW):
            if result.eligible and result.candidate.action is not ActionType.NO_ACTION:
                out.append((plan_from_candidate(result.candidate, priority_score=50.0, confidence=0.5), ctx))
    return out


@pytest.fixture(scope="module")
def current(expanded: dict[str, Any]) -> dict[str, tuple[DecisionPlan, CandidateGenerationContext, dict[str, Any]]]:
    plans = [("seed", p, c) for p, c in seed_candidate_plans()] + [("expanded", p, c) for p, c in all_candidate_plans(expanded)]
    return {key(dataset, plan): (plan, ctx, row(compose(plan, ctx))) for dataset, plan, ctx in plans}


def baseline_rows(predicate) -> list[dict[str, Any]]:
    rows = [r for r in BASELINE["rows"] if predicate(r)]
    assert rows
    return rows


def numbers(text: str) -> list[str]:
    return sorted(re.findall(r"\d+", text))


def lead(body: str) -> str:
    """The greeting: everything up to and including ``<business> here.``."""
    return body[: body.index(" here. ") + len(" here.")]


# --------------------------------------------------------------------------- #
# Golden cases (frozen Phase 3D cases T07, T28, T04, T15, T03, T13)
# --------------------------------------------------------------------------- #


def test_t07_hindi_refill_reminder_is_hinglish() -> None:
    plan, ctx = seed_plan(REFILL)
    message = compose(plan, ctx)

    assert message.body == (
        "Namaste Mr. Sharma, Apollo Health Plus Pharmacy here. Aapki medicines (metformin, atorvastatin and telmisartan) "
        "28 Apr 2026 ko khatam hongi. Last refill 26 Mar 2026 ko hui thi. Aapka delivery address humare paas saved hai. "
        "Reply CONFIRM to arrange your refill."
    )
    assert (message.send_as, plan.cta_type) == (SendAs.MERCHANT_ON_BEHALF, CTAType.CONFIRMATION)
    assert message.template_params == ("Mr. Sharma", "Apollo Health Plus Pharmacy", "metformin, atorvastatin and telmisartan",
                                       "28 Apr 2026", "26 Mar 2026")
    assert not [w for w in INTERNALS if w in message.body]


def test_t28_hi_en_recall_reminder_is_hinglish() -> None:
    plan, ctx = seed_plan(RECALL)
    message = compose(plan, ctx)

    assert message.body == (
        "Namaste Priya, Dr. Meera's Dental Clinic here. Aapke liye 6 month cleaning 12 Nov 2026 ko due hai. "
        "Last service 12 May 2026 ko hui thi. Abhi Dental Cleaning @ \u20b9299 offer chal raha hai. "
        "Aapke liye slots ready hain: Wed 5 Nov, 6pm or Thu 6 Nov, 5pm. Reply CONFIRM to book Wed 5 Nov, 6pm."
    )
    assert (message.send_as, plan.cta_type) == (SendAs.MERCHANT_ON_BEHALF, CTAType.CONFIRMATION)
    assert message.template_params == ("Priya", "Dr. Meera's Dental Clinic", "6 month cleaning", "12 Nov 2026", "12 May 2026",
                                       "Dental Cleaning @ \u20b9299", "Wed 5 Nov, 6pm or Thu 6 Nov, 5pm", "Wed 5 Nov, 6pm")
    assert not [w for w in INTERNALS if w in message.body]


@pytest.mark.parametrize(("trigger_id", "language", "expected"), [
    ("trg_077_appointment_tomorrow_m_020_renu_salon_luc", "hi",  # T04
     "Namaste Riya, Beauty Lounge by Renu here. Kal aapka appointment hai. Aapki last visit 1 Apr 2026 ko thi. "
     "Reply CONFIRM to keep your appointment."),
    ("trg_072_customer_lapsed_soft_m_049_komal_pharmaci", "hi-en mix",  # T15
     "Namaste Reyansh, Daily Care Medicos here. Aapki last visit 1 Apr 2026 ko thi, kaafi time ho gaya \u2014 koi pressure nahi. "
     "Want us to help with your next order?"),
    ("trg_076_appointment_tomorrow_m_019_karim_salon_lu", "en",  # T03: unchanged
     "Hi Aditya, Karim's Salon here. This is a reminder about your appointment tomorrow. "
     "Your last visit with us was on 1 Apr 2026. Reply CONFIRM to keep your appointment."),
])
def test_language_preference_cases(expanded: dict[str, Any], trigger_id: str, language: str, expected: str) -> None:
    ctx = expanded_context(expanded, trigger_id)
    assert ctx.customer["identity"]["language_pref"] == language
    assert compose(select(ctx), ctx).body == expected


def test_t13_english_winback_is_unchanged() -> None:
    plan, ctx = seed_plan("trg_015_winback_rashmi")
    assert ctx.customer["identity"]["language_pref"] == "english"
    assert compose(plan, ctx).body == (
        "Hi Rashmi, PowerHouse Fitness here. It's been 57 days since your last visit \u2014 no pressure at all. "
        "We're here whenever you want to get back to your weight loss goals. Want to book a session?"
    )


# --------------------------------------------------------------------------- #
# Eligibility: only customer language_pref, only merchant_on_behalf
# --------------------------------------------------------------------------- #


def with_language(trigger_id: str, language: Any) -> tuple[DecisionPlan, CandidateGenerationContext]:
    parts = seed_context_parts(trigger_id)
    customer = copy.deepcopy(parts["customer"])
    customer["identity"]["language_pref"] = language
    ctx = CandidateGenerationContext(**{**parts, "customer": customer}, now=SEED_NOW)
    return select(ctx), ctx


ENGLISH_T28 = (
    "Hi Priya, Dr. Meera's Dental Clinic here. Your 6 month cleaning is due on 12 Nov 2026. The last one was on 12 May 2026. "
    "Current offer: Dental Cleaning @ \u20b9299. Open slots: Wed 5 Nov, 6pm or Thu 6 Nov, 5pm. Reply CONFIRM to book Wed 5 Nov, 6pm."
)


@pytest.mark.parametrize("language", ["en", "english", "ta-en mix", "kn-en mix", "", None, "Hindi", "HI"])
def test_other_language_preferences_keep_english(language: Any) -> None:
    assert compose(*with_language(RECALL, language)).body == ENGLISH_T28


@pytest.mark.parametrize("language", HINDI_PREFS)
def test_both_hindi_preferences_get_hinglish(language: str) -> None:
    body = compose(*with_language(RECALL, language)).body
    assert body.startswith("Namaste Priya, Dr. Meera's Dental Clinic here. Aapke liye 6 month cleaning 12 Nov 2026 ko due hai.")


def test_language_is_never_inferred_from_merchant_city_or_name() -> None:
    parts = seed_context_parts(RECALL)
    merchant = copy.deepcopy(parts["merchant"])
    merchant["identity"]["languages"] = ["hi"]
    customer = copy.deepcopy(parts["customer"])
    customer["identity"] |= {"language_pref": "en", "name": "Ramesh Sharma"}
    ctx = CandidateGenerationContext(**{**parts, "merchant": merchant, "customer": customer}, now=SEED_NOW)
    body = compose(select(ctx), ctx).body
    assert body == ENGLISH_T28.replace("Hi Priya", "Hi Ramesh Sharma")
    assert not prefers_hindi(None) and not prefers_hindi({"identity": {}})


def test_child_profile_greets_the_parent_and_names_the_child() -> None:
    parts = seed_context_parts("trg_017_kids_yoga_trial_followup_karthik")
    customer = copy.deepcopy(parts["customer"])
    customer["identity"]["language_pref"] = "hi"
    parts |= {"customer": customer, "trigger": appointment_trigger(customer["customer_id"], parts["merchant"]["merchant_id"], {})}
    ctx = CandidateGenerationContext(**parts, now=SEED_NOW)
    body = compose(select(ctx), ctx).body
    assert body.startswith("Namaste Sumitra, Zen Yoga Studio here. Kal Karthik ka appointment hai.")
    assert body.endswith(" Reply CONFIRM to keep your appointment.")


# --------------------------------------------------------------------------- #
# Non-regression against the pre-change composer output
# --------------------------------------------------------------------------- #


def test_merchant_messages_are_byte_for_byte_unchanged(current) -> None:
    rows = baseline_rows(lambda r: r["scope"] == "merchant")
    assert len(rows) == 154
    for expected in rows:
        assert current[expected["key"]][2] == {f: expected[f] for f in FIELDS}, expected["key"]


def test_non_hindi_customer_messages_are_byte_for_byte_unchanged(current) -> None:
    rows = baseline_rows(lambda r: r["scope"] == "customer" and r["language_pref"] not in HINDI_PREFS)
    assert {r["language_pref"] for r in rows} == {"en", "english", "ta-en mix"}
    for expected in rows:
        assert current[expected["key"]][2] == {f: expected[f] for f in FIELDS}, expected["key"]


def test_every_composition_is_covered_by_the_baseline(current) -> None:
    assert set(current) == {r["key"] for r in BASELINE["rows"]}


def test_hindi_customer_messages_change_only_their_wording(current) -> None:
    rows = baseline_rows(lambda r: r["scope"] == "customer" and r["language_pref"] in HINDI_PREFS)
    assert len(rows) == 15
    for expected in rows:
        plan, ctx, after = current[expected["key"]]
        assert after["body"] != expected["body"] and HINGLISH_WORDS.search(after["body"]), after["body"]
        for field in ("template_params", "facts_used", "cta", "send_as", "template_name"):
            assert after[field] == expected[field], (expected["key"], field)
        assert lead(after["body"]) == lead(expected["body"])
        assert last_sentence(after["body"]) == last_sentence(expected["body"])
        assert numbers(after["body"]) == numbers(expected["body"])
        assert not [w for w in INTERNALS if w in after["body"].replace(lead(after["body"]), "")], after["body"]
        assert plan.send_as is SendAs.MERCHANT_ON_BEHALF and plan.scope is DecisionScope.CUSTOMER


def test_every_value_is_rendered_verbatim(current) -> None:
    for plan, _, after in current.values():
        text = after["template"]
        for index, value in enumerate(after["template_params"], start=1):
            text = text.replace(f"{{{{{index}}}}}", value, 1)
        assert text == after["body"]
        assert all(value in after["body"] for value in after["template_params"])


# --------------------------------------------------------------------------- #
# CTA contract
# --------------------------------------------------------------------------- #


def test_ctas_keep_their_type_wording_and_placement(current) -> None:
    for plan, _, after in current.values():
        if plan.scope is not DecisionScope.CUSTOMER:
            continue
        body, cta = after["body"], last_sentence(after["body"])
        assert "CANCEL" not in body
        if plan.cta_type is CTAType.CONFIRMATION:
            assert cta.startswith("Reply CONFIRM") and "?" not in body
        elif plan.cta_type is CTAType.YES_NO:
            assert cta.endswith("?") and body.count("?") == 1
        elif plan.cta_type is CTAType.OPEN_ENDED:
            assert cta == "What time works best for you?"
        else:
            assert "?" not in body and "Reply CONFIRM" not in body


# --------------------------------------------------------------------------- #
# Phrase table: same values, same Phase 3C fact kinds in both languages
# --------------------------------------------------------------------------- #

SAMPLE_VALUES = ("6 month cleaning", "12 Nov 2026, 6pm", "metformin")


def kinds(sentence: str) -> set[str]:
    return {kind.value for kind, pattern in SENTENCE_KINDS if pattern.search(sentence)}


@pytest.mark.parametrize("phrase", sorted(CUSTOMER_PHRASES))
@pytest.mark.parametrize("whose", [("Your", "your", "Aapka", "aapka", "Aapki", "Aapke"),
                                   ("Karthik's", "Karthik's", "Karthik ka", "Karthik ka", "Karthik ki", "Karthik ke")])
def test_both_languages_carry_the_same_values_and_fact_kinds(phrase: str, whose: tuple[str, ...]) -> None:
    tokens = ("{Your}", "{your}", "{Aapka}", "{aapka}", "{Aapki}", "{Aapke}")
    english, hinglish = CUSTOMER_PHRASES[phrase]
    for token, word in zip(tokens, whose, strict=True):
        english, hinglish = english.replace(token, word), hinglish.replace(token, word)
    assert english.count("{}") == hinglish.count("{}")
    values = SAMPLE_VALUES[: english.count("{}")]
    assert kinds(english.format(*values)) == kinds(hinglish.format(*values)), phrase
    assert not re.search(r"\{\w+\}", english + hinglish)


# --------------------------------------------------------------------------- #
# Decisions, determinism and the tick path
# --------------------------------------------------------------------------- #


def test_tick_decisions_do_not_depend_on_customer_language() -> None:
    def actions(language: str) -> list[dict[str, Any]]:
        state = seeded()
        for customer_id in (PRIYA, "c_013_grandfather_for_m009"):
            payload = seed_payload("customer", customer_id)
            payload["identity"]["language_pref"] = language
            push(state, "customer", customer_id, payload, version=2)
        result = tick(state, RECALL, REFILL)
        return [a.model_dump(mode="json", exclude={"body"}) for a in result.actions]

    assert actions("hi-en mix") == actions("en")


def test_same_inputs_compose_identically(current) -> None:
    for plan, ctx, after in current.values():
        before = json.dumps(plan.model_dump(mode="json"), sort_keys=True)
        assert row(compose(plan, ctx)) == after
        assert json.dumps(plan.model_dump(mode="json"), sort_keys=True) == before


def test_tick_is_deterministic() -> None:
    first = [a.model_dump(mode="json") for a in tick(seeded(), RECALL, REFILL).actions]
    second = [a.model_dump(mode="json") for a in tick(seeded(), RECALL, REFILL).actions]
    assert first == second and len(first) == 2


# --------------------------------------------------------------------------- #
# Phase 3B/3C replies on a Hinglish opener
# --------------------------------------------------------------------------- #


def opened_with(language: str, trigger_id: str = RECALL, trigger: dict[str, Any] | None = None):
    state = seeded()
    payload = seed_payload("customer", PRIYA)
    payload["identity"]["language_pref"] = language
    push(state, "customer", PRIYA, payload, version=2)
    if trigger:
        push(state, "trigger", trigger["id"], trigger)
        trigger_id = trigger["id"]
    [action] = tick(state, trigger_id).actions
    return state, action


def reply(language: str, message: str, trigger: dict[str, Any] | None = None) -> str:
    state, action = opened_with(language, trigger=trigger)
    return answer(state, action, message).response.body


BOOKED = appointment_trigger(PRIYA, MEERA, {"appointment_iso": "2026-04-27T18:00:00+05:30", "service": "cleaning"})


@pytest.mark.parametrize(("message", "hinglish", "english"), [
    ("How much is the cleaning?", "Our current offer is Dental Cleaning @ \u20b9299.", "Our current offer is Dental Cleaning @ \u20b9299."),
    ("Do you have any evening slots?", "Yes \u2014 we have Wed 5 Nov, 6pm or Thu 6 Nov, 5pm open.",
     "Yes \u2014 we have Wed 5 Nov, 6pm or Thu 6 Nov, 5pm open."),
    ("When is my cleaning due?", "Aapke liye 6 month cleaning 12 Nov 2026 ko due hai.", "Your 6 month cleaning is due on 12 Nov 2026."),
    ("When did I last come in?", "Last service 12 May 2026 ko hui thi.", "The last one was on 12 May 2026."),
    ("When is my appointment?", "We don't have a booked appointment on file.", "We don't have a booked appointment on file."),
])
def test_replies_answer_the_question_from_the_hinglish_opener(message: str, hinglish: str, english: str) -> None:
    assert reply("hi-en mix", message).startswith(hinglish)
    assert reply("en", message).startswith(english)


def test_booked_appointment_is_answered_from_the_hinglish_opener() -> None:
    assert reply("hi-en mix", "When is my appointment?", BOOKED).startswith("Aapka cleaning appointment 27 Apr 2026, 6pm ko hai.")
    assert reply("en", "When is my appointment?", BOOKED).startswith("Your cleaning appointment is on 27 Apr 2026, 6pm.")


@pytest.mark.parametrize(("trigger", "expected"), [
    (None, "Great. Reply CONFIRM and we'll book Wed 5 Nov, 6pm, or STOP to end here."),
    (BOOKED, "Great. Reply CONFIRM and we'll keep your appointment, or STOP to end here."),
])
def test_yes_continues_the_same_offer(trigger: dict[str, Any] | None, expected: str) -> None:
    assert reply("hi-en mix", "yes", trigger) == reply("en", "yes", trigger) == expected


def test_missing_price_is_still_not_guessed() -> None:
    state = seeded()
    [action] = tick(state, REFILL).actions
    assert action.body.startswith("Namaste Mr. Sharma")
    body = answer(state, action, "How much will it cost?").response.body
    assert body.startswith("We don't have the price on file.") and "\u20b9" not in body
