"""Phase 3B reply composer: a Phase 2F SEND decision -> the words Vera (or the business) sends.

    ReplyDecision (Phase 2F: intent, send/wait/end, cta, state, suppression)
        + the conversation (earlier Vera turns, trigger id, participants)
        + stored contexts (trigger kind, merchant, category, customer)
        -> ComposedReply(body, cta, template_name, template_params, template, sources)

Phase 2F decides what happens; this module only words a ``send``. It never
reclassifies, changes the action, CTA, state, wait or suppression. ``wait`` and
``end`` carry no body on the wire (challenge-testing-brief §2.3), so they pass
through untouched.

Grounding. A reply may only use:

* the topic of the conversation's trigger (a fixed phrase per trigger kind);
* sentences of the tick message that opened the conversation (already
  grounded by the Phase 3A composer): the first one as the reason, and the one
  that answers a question (Phase 3C, ``reply_facts``);
* for a customer, the few context fields ``reply_facts`` reads verbatim
  (active offers, slots, due date, appointment, last visit);
* the pending offer in the latest Vera turn ("I can draft …", "Reply CONFIRM
  to book …"), i.e. what Vera already proposed;
* identity from context (salutation / greeting) for the first Vera turn of a
  conversation opened by a reply.

Anything missing is left out; fixed template text contains no facts or numbers.

Voice follows the sender: a merchant hears Vera ("I"), a customer hears the
business ("we"), never Vera or any internal detail. Bodies never repeat an
earlier Vera turn of the same conversation (api-call-examples F.5).
Deterministic: no clock, randomness or I/O beyond reading the given stores.
"""

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from app.engine.archetypes import canonical_trigger_kind
from app.engine.composer import TemplateWriter, customer_greeting, join_words, merchant_salutation
from app.engine.reply_facts import (
    Answer,
    Fact,
    FactKind,
    customer_context_facts,
    opener_body,
    opener_facts,
    opener_sentences,
    select,
    sentences,
)
from app.models.enums import ContextScope, CtaType, TurnRole
from app.models.schemas import SendReply
from app.state.context_store import ContextStore
from app.state.conversation_store import Conversation

if TYPE_CHECKING:
    from app.engine.reply import ReplyDecision

TEMPLATE_VERSION = "v1"


class Voice(StrEnum):
    VERA = "vera"
    """Vera speaking to the merchant."""
    BUSINESS = "business"
    """The merchant's business speaking to its customer (merchant_on_behalf)."""


@dataclass(frozen=True, slots=True)
class ComposedReply:
    """A worded ``send``. ``template`` is ``body`` with ``{{n}}`` in place of each ``template_params`` entry."""

    body: str
    cta: CtaType
    template_name: str
    template_params: tuple[str, ...]
    template: str
    sources: tuple[str, ...]
    """Where each inserted value came from: ``turn:<index>``, ``trigger:kind`` or a context field."""


@dataclass(frozen=True, slots=True)
class ReplyContext:
    """Everything the composer may use for one reply, already extracted and grounded."""

    voice: Voice
    topic: str | None = None
    offer: str | None = None
    offer_source: str | None = None
    reason: str | None = None
    greeting: tuple[str, tuple[str, ...], tuple[str, ...]] | None = None
    """``(pattern, values, refs)``; only when Vera has not spoken in this conversation yet."""
    asks_why: bool = False
    earlier_bodies: frozenset[str] = frozenset()
    answer: Answer | None = None
    """The facts the inbound question asked for (Phase 3C); ``None`` when it asked for none."""
    fresh_fact: str | None = None
    """The first opener fact no Vera reply has quoted yet, for general questions."""


# --------------------------------------------------------------------------- #
# Topics: what the conversation is about, named per trigger kind (no facts)
# --------------------------------------------------------------------------- #

MERCHANT_TOPICS: dict[str, str] = {
    "regulation_change": "the compliance update",
    "supply_alert": "the recall alert",
    "active_planning_intent": "your plan",
    "curious_ask_due": "this week's question",
    "dormant_with_vera": "your account",
    "recall_due": "the recall reminders",
    "appointment_tomorrow": "the appointment reminders",
    "trial_followup": "the trial follow-ups",
    "chronic_refill_due": "the refill reminders",
    "customer_lapsed_soft": "the lapsed customers",
    "customer_lapsed_hard": "the lapsed customers",
    "wedding_package_followup": "the bridal follow-up",
    "perf_dip": "the dip in your numbers",
    "perf_spike": "the jump in your numbers",
    "seasonal_perf_dip": "the seasonal dip",
    "milestone_reached": "your milestone",
    "research_digest": "the research update",
    "festival_upcoming": "the upcoming festival",
    "category_seasonal": "the seasonal demand shift",
    "ipl_match_today": "the match-day plan",
    "cde_opportunity": "the CDE session",
    "competitor_opened": "the new competitor nearby",
    "renewal_due": "your renewal",
    "gbp_unverified": "your listing verification",
    "winback_eligible": "your subscription",
    "review_theme_emerged": "the review theme",
}

CUSTOMER_TOPICS: dict[str, str] = {
    "recall_due": "your upcoming visit",
    "appointment_tomorrow": "your appointment",
    "trial_followup": "your trial follow-up",
    "chronic_refill_due": "your refill",
    "customer_lapsed_soft": "your next visit",
    "customer_lapsed_hard": "your next visit",
    "wedding_package_followup": "your wedding prep",
}


# --------------------------------------------------------------------------- #
# Wording tables (fixed text; no facts, no numbers)
# --------------------------------------------------------------------------- #

LEADS: dict[tuple[str, Voice], tuple[str, ...]] = {
    ("affirmative", Voice.VERA): ("Great.", "Noted.", "Understood."),
    ("affirmative", Voice.BUSINESS): ("Great.", "Noted.", "Wonderful."),
    ("question", Voice.VERA): ("Good question.", "Fair question.", "Happy to help."),
    ("question", Voice.BUSINESS): ("Good question.", "Happy to help.", "Thanks for asking."),
    ("answer", Voice.VERA): ("", "Sure.", "Happy to clarify."),
    ("answer", Voice.BUSINESS): ("", "Sure.", "Happy to help."),
    ("why", Voice.VERA): ("Here's why I raised it:", "Here's what prompted it:", "The reason I raised it:"),
    ("why", Voice.BUSINESS): ("Here's why we reached out:", "Here's what prompted it:", "The reason we reached out:"),
    ("objection", Voice.VERA): ("Fair point.", "Understood.", "That's fair."),
    ("objection", Voice.BUSINESS): ("Understood.", "That's fair.", "Fair point."),
    ("off_topic", Voice.VERA): ("That's outside my scope here.", "That one isn't something I handle.", "That's not something I cover."),
    ("off_topic", Voice.BUSINESS): ("That's outside what our team handles here.", "That isn't something we handle.", "That's not something we cover."),
    ("unclear", Voice.VERA): ("Just to check \u2014", "Quick check \u2014", "Sorry, I didn't quite catch that \u2014"),
    ("unclear", Voice.BUSINESS): ("Just to check \u2014", "Quick check \u2014", "Sorry, we didn't quite catch that \u2014"),
    ("auto_reply", Voice.VERA): (
        "Looks like an automated reply. When the owner sees this,",
        "This looks like an auto-reply. When the owner sees this,",
        "Seems this is an automated message. When the owner sees this,",
    ),
    ("auto_reply", Voice.BUSINESS): (
        "Looks like an automated reply. Whenever you see this,",
        "This looks like an auto-reply. Whenever you see this,",
        "Seems this is an automated message. Whenever you see this,",
    ),
}
PREFIX_LEADS = frozenset({"unclear", "auto_reply"})
"""Intents whose lead runs into the CTA question (``Just to check — want me to …?``)."""

OPEN_QUESTIONS: dict[tuple[str, Voice], str] = {
    ("question", Voice.VERA): "What should I cover first?",
    ("why", Voice.VERA): "Which part should I start with?",
    ("objection", Voice.VERA): "What would make it worthwhile for you?",
    ("off_topic", Voice.VERA): "What would you like to do next?",
    ("answer", Voice.VERA): "What would you like to do next?",
    ("question", Voice.BUSINESS): "What works best for you?",
    ("answer", Voice.BUSINESS): "What works best for you?",
    ("why", Voice.BUSINESS): "What works best for you?",
    ("objection", Voice.BUSINESS): "What would work better for you?",
    ("off_topic", Voice.BUSINESS): "What works for you?",
}

PRONOUN = {Voice.VERA: ("I", "I'll"), Voice.BUSINESS: ("We", "we'll")}

MISSING: dict[str, str] = {
    "price": "the price",
    "availability": "open slots",
    "appointment": "a booked appointment",
    "due": "a due date",
    "deadline": "a deadline",
    "count": "that number",
    "change": "that figure",
    "source": "a source",
    "last_visit": "a previous visit",
    "date": "a date",
}
"""What the recipient asked for, named when no grounded fact answers it."""

STOP_WORD = "STOP"
"""The challenge brief's binary exit word (``YES/STOP``); Phase 2F reads it as an opt-out and ends the conversation."""


# --------------------------------------------------------------------------- #
# Extraction from the conversation (grounded text only)
# --------------------------------------------------------------------------- #

OFFER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:^|(?<=\s))(?:I|[Ww]e) can (?P<x>.+?)\.(?=\s|$)"), "{}"),
    (re.compile(r"Reply CONFIRM and (?:I|we)'ll (?P<x>.+?), or (?:CANCEL|STOP) to (?:stop|end here)\."), "{}"),
    (re.compile(r"Reply CONFIRM and I'll (?P<x>.+?)\.(?=\s|$)"), "{}"),
    (re.compile(r"Reply CONFIRM to (?P<x>.+?)\.(?=\s|$)"), "{}"),
    (re.compile(r"(?:Want me to|Want us to|[Ss]hall we|Want to) (?P<x>.+?)(?: for you)?\?"), "{}"),
    (re.compile(r"Want (?P<x>the [^?]+)\?"), "share {}"),
)
"""How Vera's own messages (Phase 3A tick messages and these replies) phrase the pending offer."""

UNUSABLE = re.compile(r"[\[\]{}?]")


def pending_offer(conversation: Conversation) -> tuple[str, str] | None:
    """The newest offer Vera made in this conversation, and the turn it came from."""
    for index in range(len(conversation.turns) - 1, -1, -1):
        turn = conversation.turns[index]
        if turn.role is not TurnRole.VERA or "[uncomposed" in turn.body:
            continue
        for pattern, shape in OFFER_PATTERNS:
            match = pattern.search(turn.body)
            if match and not UNUSABLE.search(match.group("x")) and len(match.group("x")) <= 200:
                return shape.format(match.group("x")), f"turn:{index}"
    return None


def opening_fact(conversation: Conversation) -> str | None:
    """The first fact of the tick message that opened the conversation, without its greeting."""
    text = opener_body(conversation)
    if text is None:
        return None
    parts = sentences(text)
    if not parts:
        return None
    first = parts[0]
    if UNUSABLE.search(first) or len(first) > 240 or first.startswith(("Reply", "I can", "Want", "Shall", "A quick update")):
        return None
    return first


def reply_context(conversation: Conversation, contexts: ContextStore) -> ReplyContext:
    """Extract what the reply may say. ``conversation`` already holds the inbound turn (its last turn)."""
    inbound = conversation.turns[-1]
    voice = Voice.BUSINESS if inbound.role is TurnRole.CUSTOMER else Voice.VERA

    def payload(scope: ContextScope, context_id: str | None) -> dict | None:
        found = contexts.get(scope, context_id) if context_id else None
        return None if found is None else found.payload

    trigger = payload(ContextScope.TRIGGER, conversation.trigger_id)
    kind = canonical_trigger_kind(str(trigger.get("kind"))) if trigger else None
    topics = CUSTOMER_TOPICS if voice is Voice.BUSINESS else MERCHANT_TOPICS
    topic = topics.get(kind) if kind else None

    greeting = None
    if not any(t.role is TurnRole.VERA for t in conversation.turns):
        merchant = payload(ContextScope.MERCHANT, conversation.merchant_id)
        category = payload(ContextScope.CATEGORY, merchant.get("category_slug")) if merchant else None
        if voice is Voice.BUSINESS and merchant is not None:
            pattern, values, refs, _ = customer_greeting(payload(ContextScope.CUSTOMER, conversation.customer_id), merchant)
            greeting = (pattern, values, refs)
        elif merchant is not None and category is not None and (salutation := merchant_salutation(merchant, category)):
            (pattern, *values), refs = salutation
            greeting = (f"{pattern} \u2014", tuple(values), refs)

    vera_turns = [t.body for t in conversation.turns if t.role is TurnRole.VERA and "[uncomposed" not in t.body]
    pool = opener_facts(conversation)
    if voice is Voice.BUSINESS:
        merchant = payload(ContextScope.MERCHANT, conversation.merchant_id)
        category = payload(ContextScope.CATEGORY, merchant.get("category_slug")) if merchant else None
        taboos = [t.lower() for t in ((category or {}).get("voice") or {}).get("vocab_taboo") or [] if isinstance(t, str)]
        pool = customer_context_facts(trigger, merchant, payload(ContextScope.CUSTOMER, conversation.customer_id), taboos) + pool
    answer = select(inbound.body, pool, vera_turns)
    replies = " ".join(vera_turns[1:]) if conversation.trigger_id else " ".join(vera_turns)

    offer = pending_offer(conversation)
    return ReplyContext(
        voice=voice,
        topic=topic,
        offer=offer[0] if offer else None,
        offer_source=offer[1] if offer else None,
        reason=opening_fact(conversation),
        greeting=greeting,
        asks_why=re.search(r"\bwhy\b", inbound.body.lower()) is not None,
        earlier_bodies=frozenset(t.body for t in conversation.turns if t.role is TurnRole.VERA),
        answer=answer if answer.asked else None,
        fresh_fact=next((s for s in opener_sentences(conversation) if s not in replies), None),
    )


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #


def compose_reply(intent: str, cta: CtaType, context: ReplyContext) -> ComposedReply:
    """Word a Phase 2F ``send`` for ``intent`` with the decided ``cta``. Pure and deterministic."""
    if intent == "question" and context.asks_why and context.reason:
        key = "why"
    elif intent == "question" and context.answer is not None:
        key = "answer"
    else:
        key = intent
    leads = LEADS.get((key, context.voice)) or LEADS[("unclear", context.voice)]
    candidate = None
    for lead in leads:
        candidate = _build(intent, key, lead, cta, context)
        if candidate.body not in context.earlier_bodies:
            return candidate
    follow_up = 2
    while True:
        extra = _build(intent, key, leads[0], cta, context, follow_up=follow_up)
        if extra.body not in context.earlier_bodies:
            return extra
        follow_up += 1


def _build(intent: str, key: str, lead: str, cta: CtaType, context: ReplyContext, follow_up: int | None = None) -> ComposedReply:
    voice = context.voice
    i, ill = PRONOUN[voice]
    writer = TemplateWriter()
    sources: list[str] = []

    def add(pattern: str, *values: str, refs: tuple[str, ...] = ()) -> None:
        writer.add(pattern, *values, facts=refs)
        sources.extend(r for r in refs if r not in sources)

    offer_ref = (context.offer_source,) if context.offer_source else ()
    topic_ref = ("trigger:kind",)

    prefix = lead if intent in PREFIX_LEADS else None
    if context.greeting is not None:
        pattern, values, refs = context.greeting
        if prefix is None and lead:
            add(f"{pattern} {lead}", *values, refs=refs)
        else:
            add(pattern, *values, refs=refs)
    elif prefix is None and lead:
        add(lead)

    def cta_sentence(pattern: str, *values: str, refs: tuple[str, ...] = ()) -> None:
        add(f"{prefix} {pattern[0].lower()}{pattern[1:]}" if prefix else pattern, *values, refs=refs)

    answered = ""
    if key == "why":
        add("{}", context.reason, refs=("turn:0",))
    elif key == "answer" and context.answer is not None:
        for pattern, values, refs in answer_lines(context.answer, voice):
            add(pattern, *values, refs=refs)
        answered = " ".join(writer.body)
    elif intent == "question" and (fact := context.fresh_fact or context.reason):
        add(("What I have on record: {}" if voice is Voice.VERA else "What we have on record: {}"), fact, refs=("turn:0",))
    elif intent == "objection" and context.reason:
        add(("I raised it because of this: {}" if voice is Voice.VERA else "We reached out because of this: {}"),
            context.reason, refs=("turn:0",))
    if intent == "objection":
        add("No pressure either way.")
    returning = intent == "off_topic" and context.topic is not None
    if returning and not (cta is CtaType.OPEN_ENDED and context.offer):
        add("Coming back to {}.", context.topic, refs=topic_ref)

    if cta is CtaType.BINARY_CONFIRM_CANCEL:
        if prefix is not None:
            add(prefix.rstrip(" ,\u2014") + ".")
            prefix = None
        stop = f", or {STOP_WORD} to end here."
        if context.offer:
            cta_sentence(f"Reply CONFIRM and {ill} {{}}{stop}", context.offer, refs=offer_ref)
        elif context.topic:
            cta_sentence(f"Reply CONFIRM and {ill} take the next step on {{}}{stop}", context.topic, refs=topic_ref)
        else:
            cta_sentence(f"Reply CONFIRM and {ill} take the next step{stop}")
    elif cta is CtaType.BINARY_YES_NO:
        ask = "Want me to" if voice is Voice.VERA else "Shall we"
        cont = "Should I continue" if voice is Voice.VERA else "Shall we continue"
        if context.offer:
            cta_sentence(f"{ask} {{}}?", context.offer, refs=offer_ref)
        elif context.topic:
            cta_sentence(f"{cont} with {{}}?", context.topic, refs=topic_ref)
        else:
            cta_sentence(f"{cont}?")
    elif cta is CtaType.OPEN_ENDED:
        if context.offer and returning:
            add(f"Coming back to {{}}: {'we' if voice is Voice.BUSINESS else 'I'} can {{}}.", context.topic, context.offer,
                refs=(*topic_ref, *offer_ref))
        elif context.offer and not (answered and context.offer.split(" ", 1)[-1] in answered):
            add(f"{i} can {{}}.", context.offer, refs=offer_ref)
        slots = key == "answer" and context.answer is not None and any(
            f.kind is FactKind.AVAILABILITY for f in (*context.answer.facts, *context.answer.fallbacks))
        cta_sentence("Which slot works best for you?" if slots and voice is Voice.BUSINESS
                     else OPEN_QUESTIONS.get((key, voice)) or OPEN_QUESTIONS[("off_topic", voice)])
    elif prefix is not None:
        add(prefix.rstrip(" ,\u2014") + ".")

    if follow_up is not None:
        writer.body[-1:] = [f"(Follow-up {follow_up}) {writer.body[-1]}"]
        writer.template[-1:] = [f"(Follow-up {follow_up}) {writer.template[-1]}"]
    return ComposedReply(
        body=writer.text,
        cta=cta,
        template_name=f"vera_reply_{key}_{TEMPLATE_VERSION}",
        template_params=tuple(writer.params),
        template=" ".join(writer.template),
        sources=tuple(sources),
    )


Line = tuple[str, tuple[str, ...], tuple[str, ...]]


def answer_lines(answer: Answer, voice: Voice) -> list[Line]:
    """Sentences answering the question: selected facts first, then what is missing and any related fallback."""
    lines = [_fact_line(fact, answer, voice) for fact in answer.facts]
    for request in answer.missing:
        noun = MISSING.get(request.name, "that")
        lines.append((f"We don't have {noun} on file." if voice is Voice.BUSINESS else f"I don't have {noun} on record.", (), ()))
    lines.extend(_fact_line(fact, answer, voice) for fact in answer.fallbacks)
    return lines


def _fact_line(fact: Fact, answer: Answer, voice: Voice) -> Line:
    refs = (fact.ref,)
    business = voice is Voice.BUSINESS
    if fact.kind is FactKind.PRICE and fact.structured:
        return ("Our current offer is {}." if business else "The current offer is {}."), (fact.values[0],), refs
    if fact.kind is FactKind.AVAILABILITY and fact.structured:
        have = "we have" if business else "there's"
        everything = join_words(fact.values, "or")
        part = answer.part_of_day
        if part and fact.ref in answer.matching_slots:
            matching = answer.matching_slots[fact.ref]
            if not matching:
                article = "an" if part[0] in "aeiou" else "a"
                subject = "We don't have" if business else "There's no"
                return f"{subject} {article} {part} slot open. The open slots are {{}}.", (everything,), refs
            everything = join_words(matching, "or")
        if answer.yes_no_form:
            return f"Yes \u2014 {have} {{}} open.", (everything,), refs
        return f"{have[0].upper()}{have[1:]} {{}} open.", (everything,), refs
    return "{}", (fact.sentence,), refs


def realize(decision: "ReplyDecision", conversation: Conversation, contexts: ContextStore) -> "ReplyDecision":
    """``decision`` with its ``send`` body worded; ``wait``/``end`` and every other field unchanged."""
    response = decision.response
    if not isinstance(response, SendReply) or decision.reading is None:
        return decision
    composed = compose_reply(decision.reading.intent, response.cta, reply_context(conversation, contexts))
    return replace(decision, response=response.model_copy(update={"body": composed.body}))


__all__ = [
    "CUSTOMER_TOPICS",
    "MERCHANT_TOPICS",
    "TEMPLATE_VERSION",
    "ComposedReply",
    "ReplyContext",
    "Voice",
    "compose_reply",
    "opening_fact",
    "pending_offer",
    "realize",
    "reply_context",
    "sentences",
]
