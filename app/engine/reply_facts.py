"""Phase 3C fact relevance: which grounded fact answers what the recipient just asked.

    inbound question
        -> requests (REQUESTS: explicit phrase patterns -> wanted fact kinds)
        -> fact pool (tick-opener sentences tagged by kind; for customers, a few
           structured context fields: offers, slots, due date, appointment, last visit)
        -> ranked per request (word overlap with the question, mentioned in the
           latest Vera turn, mentioned earlier, structured before sentence, order)
        -> Answer(facts, missing requests, requested part of day)

Wording only: nothing here reads or changes a Phase 2F decision. Every fact is
either a sentence Vera already sent (grounded by the Phase 3A composer, already
filtered for its audience) or a value read verbatim from the stored contexts.
Merchant conversations use opener sentences only; merchant-internal context is
never offered to a customer. Deterministic: no clock, randomness or I/O.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.engine.composer import humanize, join_words
from app.models.enums import TurnRole
from app.state.conversation_store import Conversation

MAX_REQUESTS = 2
"""At most this many requested facts are answered in one reply."""


class FactKind(StrEnum):
    PRICE = "price"
    AVAILABILITY = "availability"
    APPOINTMENT = "appointment"
    DUE_DATE = "due_date"
    DEADLINE = "deadline"
    EVENT_DATE = "event_date"
    LAST_VISIT = "last_visit"
    COUNT = "count"
    CHANGE = "change"
    SOURCE = "source"


K = FactKind


@dataclass(frozen=True, slots=True)
class Fact:
    kind: FactKind
    sentence: str
    """A complete sentence that states the fact on its own."""
    ref: str
    """``turn:0`` for opener sentences, ``<scope>:<path>`` for context fields."""
    values: tuple[str, ...] = ()
    """Structured values (offer titles, slot labels) when read from context."""
    slot_times: tuple[datetime | None, ...] = ()
    """Start time per slot value, when the context gives one."""

    @property
    def structured(self) -> bool:
        return bool(self.values)


@dataclass(frozen=True, slots=True)
class Request:
    name: str
    kinds: tuple[FactKind, ...]
    pattern: re.Pattern[str]
    generic: bool = False
    """Dropped when a specific request is also present (``when`` vs ``when is it due``)."""


def _words(*alternatives: str) -> re.Pattern[str]:
    return re.compile("|".join(alternatives))


REQUESTS: tuple[Request, ...] = (
    Request("last_visit", (K.LAST_VISIT,), _words(r"\blast (?:visit|time|refill|cleaning|appointment|service)\b", r"\bwhen did (?:i|we)\b")),
    Request("appointment", (K.APPOINTMENT,), _words(r"\bappointment\b", r"\bbooking\b", r"\bbooked\b")),
    Request("availability", (K.AVAILABILITY,), _words(
        r"\bslots?\b", r"\bavailab(?:le|ility)\b", r"\btimings?\b", r"\bwhat times?\b", r"\bwhich (?:times?|days?)\b",
        r"\bwhen can (?:i|we)(?: (?:come|visit|book|drop by))?\b", r"\b(?:mornings?|afternoons?|evenings?|weekends?)\b",
    )),
    Request("price", (K.PRICE,), _words(
        r"(?<!by )\bhow much\b", r"\bcosts?\b", r"\bprices?\b", r"\bpricing\b", r"\bcharges?\b", r"\bfees?\b",
        r"\brates?\b", r"\brupees\b", r"\bkitn[ae]\b", "\u20b9",
    )),
    Request("due", (K.DUE_DATE,), _words(r"\bdue\b", r"\bruns? out\b", r"\brenewal\b")),
    Request("deadline", (K.DEADLINE, K.DUE_DATE), _words(r"\bdeadline\b", r"\bby when\b", r"\blast date\b")),
    Request("count", (K.COUNT,), _words(r"\bhow many\b", r"\bnumber of\b")),
    Request("change", (K.CHANGE,), _words(r"\bby how much\b", r"\bhow (?:bad|big)\b", r"\bpercent(?:age)?\b", "%")),
    Request("source", (K.SOURCE,), _words(
        r"\bsource\b", r"\bwhere (?:is|was|did|does) (?:this|that|it)\b", r"\bwhich (?:study|journal|paper)\b", r"\bcitation\b",
    )),
    Request("date", (K.EVENT_DATE, K.DUE_DATE, K.DEADLINE, K.APPOINTMENT), _words(r"\bwhen\b", r"\bwhat (?:date|day)\b"), generic=True),
)
"""Checked against the lowercased question. Overlapping matches keep the earliest, then longest, then first declared."""

RELATED: dict[str, tuple[FactKind, ...]] = {"appointment": (K.AVAILABILITY,)}
"""A grounded fallback worth offering when the requested fact is missing."""

PARTS_OF_DAY: dict[str, tuple[int, int]] = {"morning": (5, 12), "afternoon": (12, 17), "evening": (17, 23)}
PART_PATTERN = re.compile(r"\b(morning|afternoon|evening|weekend)s?\b")

PRICE_POINT = re.compile(r"@\s*(?:\u20b9|Rs\.?|INR)\s?\d")
"""``Haircut @ ₹99``: the price-point form of offer titles (challenge-brief §5.5). Thresholds like ``> ₹499`` are not prices."""

STRUCTURED_FIRST = frozenset({FactKind.PRICE, FactKind.AVAILABILITY})
"""Kinds answered better from structured values; other kinds prefer the opener's own sentence."""

ITEM_KINDS = frozenset({FactKind.PRICE})
"""Kinds tied to a named item: if the question names one ("the cleaning") and no fact mentions it, none answers."""

SENTENCE_KINDS: tuple[tuple[FactKind, re.Pattern[str]], ...] = (
    (K.PRICE, re.compile(rf"{PRICE_POINT.pattern}|^(?:Fee|Renewal amount):")),
    (K.AVAILABILITY, re.compile(r"^(?:Open slots|Upcoming session):")),
    (K.APPOINTMENT, re.compile(r"\bappointment is\b")),
    (K.DUE_DATE, re.compile(r"\bdue on\b|\brun out on\b|\bdue for (?:renewal|a refill)\b")),
    (K.DEADLINE, re.compile(r"^Deadline:|\beffective\b")),
    (K.EVENT_DATE, re.compile(r"^(?:Date|Start time|Opened):|\bis coming up\b|\bwedding (?:is )?on\b|\btrial on\b")),
    (K.LAST_VISIT, re.compile(r"\blast (?:one|visit)\b|^Last refill:")),
    (K.COUNT, re.compile(
        r"\b\d[\d,]*\s+(?:[\w-]+\s+){0,2}(?:patients|customers|members|clients|orders|calls|views|reviews|times|batches)\b"
    )),
    (K.CHANGE, re.compile(r"\b(?:up|down) \d+(?:\.\d+)?%|\bunchanged\b")),
    (K.SOURCE, re.compile(r"^(?:Source|Issued by|Organiser):")),
)
"""How Phase 3A sentences state each kind of fact (see ``composer.MERCHANT_PHRASES`` and the customer message)."""

NOT_FACTS = ("Reply", "I can", "We can", "Want", "Shall", "Should", "What", "A quick update")
UNUSABLE = re.compile(r"[\[\]{}?]")
TITLES = ("Dr.", "Mr.", "Mrs.", "Ms.")
CONTENT_WORD = re.compile(r"[a-z]{4,}")
STOPWORDS = frozenset({"what", "when", "have", "does", "your", "this", "that", "with", "there", "much", "many", "about", "will", "would", "could", "tell"})


# --------------------------------------------------------------------------- #
# Opener sentences
# --------------------------------------------------------------------------- #


def sentences(text: str) -> list[str]:
    """Split on sentence ends, keeping honorifics like ``Dr.`` attached."""
    out, start = [], 0
    for match in re.finditer(r"[.!?\u201d]\s+", text):
        end = match.start() + 1
        if text[:end].endswith(TITLES):
            continue
        out.append(text[start:end].strip())
        start = match.end()
    tail = text[start:].strip()
    return [*out, tail] if tail else out


def opener_body(conversation: Conversation) -> str | None:
    """The tick message that opened the conversation, without its salutation or greeting."""
    if conversation.trigger_id is None or not conversation.turns:
        return None
    opener = conversation.turns[0]
    if opener.role is not TurnRole.VERA or "[uncomposed" in opener.body:
        return None
    text = opener.body
    if " \u2014 " in text[:60]:
        return text.split(" \u2014 ", 1)[1]
    if (here := text.find(" here. ")) != -1 and here < 120:
        return text[here + len(" here. "):]
    return text


def opener_sentences(conversation: Conversation) -> list[str]:
    """The opener's fact sentences: no offers, CTAs, questions or markup."""
    text = opener_body(conversation)
    if text is None:
        return []
    return [s for s in sentences(text) if not UNUSABLE.search(s) and len(s) <= 240 and not s.startswith(NOT_FACTS)]


def opener_facts(conversation: Conversation) -> list[Fact]:
    return [
        Fact(kind, sentence, "turn:0")
        for sentence in opener_sentences(conversation)
        for kind, pattern in SENTENCE_KINDS
        if pattern.search(sentence)
    ]


# --------------------------------------------------------------------------- #
# Structured context facts (customer conversations only)
# --------------------------------------------------------------------------- #


def _date(value: Any) -> str | None:
    return humanize(value) if isinstance(value, str) and value and humanize(value) != value else None


def _start(item: Mapping[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(item.get("iso")).replace("Z", "+00:00"))
    except ValueError:
        return None


def customer_context_facts(
    trigger: Mapping[str, Any] | None, merchant: Mapping[str, Any] | None, customer: Mapping[str, Any] | None, taboos: Sequence[str]
) -> list[Fact]:
    """What a business may tell its own customer, read verbatim from the contexts."""
    payload = (trigger or {}).get("payload") or {}
    facts: list[Fact] = []

    for key in ("available_slots", "next_session_options"):
        slots = [s for s in payload.get(key) or [] if isinstance(s, Mapping) and (s.get("label") or s.get("iso"))]
        if slots:
            labels = tuple(humanize(str(s.get("label") or s.get("iso"))) for s in slots)
            facts.append(Fact(K.AVAILABILITY, f"Open slots: {join_words(labels, 'or')}.", f"trigger:payload.{key}",
                              labels, tuple(_start(s) for s in slots)))
    if appointment := _date(payload.get("appointment_iso")):
        facts.append(Fact(K.APPOINTMENT, f"Your appointment is on {appointment}.", "trigger:payload.appointment_iso", (appointment,)))
    if due := _date(payload.get("due_date")):
        facts.append(Fact(K.DUE_DATE, f"It's due on {due}.", "trigger:payload.due_date", (due,)))
    if runs_out := _date(payload.get("stock_runs_out_iso")):
        facts.append(Fact(K.DUE_DATE, f"The current stock runs out on {runs_out}.", "trigger:payload.stock_runs_out_iso", (runs_out,)))
    for path, value in (("trigger:payload.last_service_date", payload.get("last_service_date")),
                        ("customer:relationship.last_visit", ((customer or {}).get("relationship") or {}).get("last_visit"))):
        if (visit := _date(value)) and not any(visit in f.values for f in facts if f.kind is K.LAST_VISIT):
            facts.append(Fact(K.LAST_VISIT, f"Your last visit was on {visit}.", path, (visit,)))
    for index, offer in enumerate((merchant or {}).get("offers") or []):
        title = offer.get("title") if isinstance(offer, Mapping) else None
        if offer.get("status") == "active" and isinstance(title, str) and PRICE_POINT.search(title):
            facts.append(Fact(K.PRICE, f"Current offer: {title}.", f"merchant:offers[{index}].title", (title,)))
    return [f for f in facts if not any(t in f.sentence.lower() for t in taboos)]


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Answer:
    requests: tuple[Request, ...]
    facts: tuple[Fact, ...] = ()
    """One per answered request, in the order the question asked for them."""
    missing: tuple[Request, ...] = ()
    fallbacks: tuple[Fact, ...] = ()
    """Related facts offered in place of missing ones (``RELATED``)."""
    part_of_day: str | None = None
    yes_no_form: bool = False
    matching_slots: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Per availability fact ref: the slot labels in the requested part of day."""

    @property
    def asked(self) -> bool:
        return bool(self.requests)


def requests_in(message: str) -> tuple[Request, ...]:
    return _read(message)[0]


def _read(message: str) -> tuple[tuple[Request, ...], frozenset[str]]:
    """The requests in ``message`` and its remaining content words (what the question is about)."""
    text = message.lower()
    matches = sorted(
        ((m.start(), -(m.end() - m.start()), order, m.end(), request)
         for order, request in enumerate(REQUESTS) for m in request.pattern.finditer(text)),
        key=lambda item: item[:3],
    )
    spans: list[tuple[int, int]] = []
    chosen: list[Request] = []
    for start, _, _, end, request in matches:
        if any(start < e and s < end for s, e in spans):
            continue
        spans.append((start, end))
        if request not in chosen:
            chosen.append(request)
    if any(not r.generic for r in chosen):
        chosen = [r for r in chosen if not r.generic]
    rest = "".join(" " if any(s <= i < e for s, e in spans) else c for i, c in enumerate(text))
    return tuple(chosen[:MAX_REQUESTS]), frozenset(CONTENT_WORD.findall(rest)) - STOPWORDS


def _mentions(fact: Fact, text: str) -> bool:
    return fact.sentence in text or (fact.structured and all(v in text for v in fact.values))


def _overlap(fact: Fact, question_words: frozenset[str]) -> int:
    return len(question_words & set(CONTENT_WORD.findall(fact.sentence.lower())))


def _in_part(start: datetime | None, part: str) -> bool:
    if start is None:
        return False
    if part == "weekend":
        return start.weekday() >= 5
    low, high = PARTS_OF_DAY[part]
    return low <= start.hour < high


def select(message: str, pool: Sequence[Fact], vera_turns: Sequence[str]) -> Answer:
    """The facts that answer ``message``, ranked deterministically; ``vera_turns`` oldest first."""
    requests, question_words = _read(message)
    if not requests:
        return Answer(())
    latest = vera_turns[-1] if vera_turns else ""
    earlier = " ".join(vera_turns)

    def best(kinds: tuple[FactKind, ...], taken: Sequence[Fact]) -> Fact | None:
        candidates = [
            (index, f) for index, f in enumerate(pool)
            if f.kind in kinds and f not in taken
            and not (f.kind in ITEM_KINDS and question_words and not _overlap(f, question_words))
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda item: (
            -_overlap(item[1], question_words),
            not _mentions(item[1], latest),
            not _mentions(item[1], earlier),
            item[1].structured != (item[1].kind in STRUCTURED_FIRST),
            kinds.index(item[1].kind),
            item[0],
        ))[1]

    chosen: list[Fact] = []
    missing: list[Request] = []
    fallbacks: list[Fact] = []
    for request in requests:
        if found := best(request.kinds, chosen):
            chosen.append(found)
        elif not any(f.kind in request.kinds for f in chosen):
            missing.append(request)
            if request.name in RELATED and (related := best(RELATED[request.name], [*chosen, *fallbacks])):
                fallbacks.append(related)

    part = next(iter(PART_PATTERN.findall(message.lower())), None)
    matching = {
        f.ref: tuple(v for v, t in zip(f.values, f.slot_times, strict=True) if _in_part(t, part))
        for f in (*chosen, *fallbacks) if part and f.kind is K.AVAILABILITY and f.slot_times
    }
    return Answer(
        requests=requests,
        facts=tuple(chosen),
        missing=tuple(missing),
        fallbacks=tuple(fallbacks),
        part_of_day=part,
        yes_no_form=re.match(r"\s*(?:do|does|is|are|any|can|could|have|has)\b", message.lower()) is not None,
        matching_slots=matching,
    )


__all__ = [
    "MAX_REQUESTS",
    "RELATED",
    "REQUESTS",
    "Answer",
    "Fact",
    "FactKind",
    "Request",
    "customer_context_facts",
    "opener_body",
    "opener_facts",
    "opener_sentences",
    "requests_in",
    "select",
    "sentences",
]
