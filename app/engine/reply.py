"""Phase 2F reply engine: rule-based reading of an inbound reply and the next conversation step.

Flow for ``POST /v1/reply`` (serialized with tick planning under ``state.tick_lock``)::

    look up / create conversation -> ownership check -> append inbound turn
        -> read message (+ the sender's auto-reply streak) -> decide
        -> state transition, suppression write, Vera turn -> response

The engine never generates candidates or runs a tick: it only continues the
conversation it was given. Semantics follow the challenge package:

* auto-reply: first one gets a single prompt, the second in a row waits 24h,
  the third ends (api-call-examples 4.1; challenge-brief §9 Pattern B, §12.1).
  The streak is the sender's, across their conversations: the canned text
  comes from the merchant's WhatsApp Business account, not from one thread.
* commitment ("let's do it", "go ahead", "yes"): switch to action mode, never
  back to qualifying (4.2; challenge-brief §9 Pattern D, §12.2).
* asked for time: wait 30 min (challenge-testing-brief §2.3).
* not interested / stop: end; the ended conversation is the suppression
  (api-call-examples 2.6, challenge-brief §12.5).
* hostile: end and suppress every trigger for the merchant (4.3). The "30 days"
  in 4.3's rationale is illustrative, so the suppression has no expiry.
* off-topic: decline politely and redirect to the original trigger (2.7, 4.3).

``decide_reply`` puts a structured instruction in a ``send`` body; ``handle_reply``
replaces it with the Phase 3B wording (``reply_composer.realize``) before the
turn is stored or returned.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.engine.eligibility import merchant_suppression_key
from app.engine.reply_composer import realize
from app.models.enums import ConversationState, CtaType, FromRole, TurnRole
from app.models.schemas import EndReply, ReplyRequest, SendReply, WaitReply
from app.state.container import StateContainer
from app.state.conversation_store import Conversation, ConversationStore

logger = logging.getLogger(__name__)

S = ConversationState

DEFER_WAIT_SECONDS = 1800
"""challenge-testing-brief §2.3: "Merchant asked for time; back off 30 min"."""

AUTO_REPLY_WAIT_SECONDS = 86400
"""api-call-examples 4.1: second auto-reply in a row -> wait 24h."""

AUTO_REPLY_END_STREAK = 3
"""api-call-examples 4.1: "Auto-reply 3x in a row, no real reply ... closing"."""

VERBATIM_AUTO_REPLY_COUNT = 3
"""challenge-brief §12.1: "same message verbatim 3+ times = auto-reply"."""

REPLY_PLACEHOLDER_PREFIX = "[uncomposed reply]"
CLOSED_RATIONALE = "Conversation already closed; reply recorded and no further messages will be sent."


class ReplyIntent(StrEnum):
    """What an inbound reply is read as, in precedence order."""

    HOSTILE = "hostile"
    OPT_OUT = "opt_out"
    AUTO_REPLY = "auto_reply"
    NOT_INTERESTED = "not_interested"
    DEFERRAL = "deferral"
    OFF_TOPIC = "off_topic"
    OBJECTION = "objection"
    AFFIRMATIVE = "affirmative"
    QUESTION = "question"
    UNCLEAR = "unclear"


def _words(*alternatives: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")\b")


R = ReplyIntent
RULES: tuple[tuple[ReplyIntent, re.Pattern[str]], ...] = (
    (R.HOSTILE, _words(
        "useless", "spam", "spamming", "bothering", "disturbing", r"harass\w*", "pathetic", "nonsense", "rubbish",
        "stupid", r"idiot\w*", "fraud", r"scam\w*", r"irritat\w*", "annoying", "shut up", "go away", "get lost",
        "fed up", "wtf", "bakwa+s", "pagal",
    )),
    (R.OPT_OUT, _words(
        "^stop", r"stop (?:messaging|sending|texting|contacting|calling|spamming|this|these|it)", "unsubscribe",
        r"opt ?-?out", "remove me", r"(?:dont|do not) (?:message|msg|contact|text|call) me", "leave me alone",
        r"no more (?:messages|msgs|texts)", "band karo", "mat bhejo",
    )),
    (R.AUTO_REPLY, _words(
        r"thank(?:s| you) for (?:contacting|reaching out|your message|messaging)",
        r"(?:we(?: will| shall|ll)|our team will) (?:\w+ )?(?:respond|reply|get back|revert|contact)",
        r"automated (?:assistant|message|reply|response)", r"auto ?-?reply", r"out of (?:the )?office",
        r"currently (?:unavailable|away|closed)", r"outside (?:our )?business hours",
        r"team tak pahuncha\w*",
    )),
    (R.NOT_INTERESTED, _words(
        "not interested", "no thanks", "no thank you", r"nahi+n? chahiye", "not needed", r"(?:dont|do not) (?:need|want)(?! to)",
        "not for me", "no need", "not required", r"interested nahi+n?",
    )),
    (R.DEFERRAL, _words(
        "later", "not now", "busy", "tomorrow", "next week", "some other time", "call me back", "get back to you",
        r"give me (?:some )?time", r"baad me+i?n", r"abhi nahi+n?", "kal",
    )),
    (R.NOT_INTERESTED, re.compile(r"^(?:no|nope|nah|nahi+n?|na)\b(?!\s*(?:problem|worries|issue|rush|doubt))")),
    (R.OFF_TOPIC, _words(
        "gst", "income tax", r"tax (?:return|filing)s?", "itr", "loan", "insurance", "passport", "visa",
        "electricity bill", "pan card", "aadha?ar", "bank account", "file my",
    )),
    (R.OBJECTION, _words(
        r"too (?:expensive|costly|much)", "expensive", "costly", "no budget", "cant afford", "not worth",
        r"waste of (?:money|time)", r"(?:doesnt|wont|didnt) work", "already tried", r"already (?:have|using|use)",
        r"mehe?nga", "why should i", "what guarantee", "not sure",
    )),
    (R.AFFIRMATIVE, _words(
        "yes", "yeah", "yep", "yup", "sure", "ok", "okay", r"okk+", r"haa*n", "go ahead", "lets do it", "lets go",
        "do it", "proceed", r"confirm(?:ed)?", "sounds good", r"please (?:send|share|do|go|proceed|start)",
        r"send (?:it|me|the)", r"i want to (?:join|start|do|try|go ahead|proceed)", "join", r"jud(?:r)?na", "jodna",
        r"i(?:m| am) in", "interested", "chalo", "kar do", r"th?e?e?k hai", "done", r"agreed?", "absolutely",
        "definitely", "why not",
    )),
    (R.AFFIRMATIVE, re.compile(r"^\d{1,2}\W*$")),
    (R.QUESTION, re.compile(
        r"\?|^(?:what|whats|why|how|when|where|which|who|can|could|will|would|is|are|do|does)\b|\b(?:kya|kaise|kitna|kitne|kab)\b"
    )),
)
"""First match wins. Negatives and deferrals precede affirmatives ("ok, but later");
objections and off-topic asks precede them too ("ok, can you file my GST?")."""

VERBATIM_ELIGIBLE = frozenset({R.OFF_TOPIC, R.OBJECTION, R.QUESTION, R.UNCLEAR})
"""Readings the verbatim-repeat rule may override: a repeated "yes" or "stop" keeps its meaning."""


def normalize(message: str) -> str:
    """Lowercase, drop apostrophes (so "let's" == "lets"), collapse whitespace."""
    text = re.sub(r"['\u2018\u2019`]", "", message.lower())
    return " ".join(text.split())


def classify_text(message: str) -> tuple[ReplyIntent, str | None]:
    """The intent of one message on its own, and the matched cue."""
    text = normalize(message)
    for intent, pattern in RULES:
        match = pattern.search(text)
        if match:
            return intent, match.group(0) or None
    return R.UNCLEAR, None


@dataclass(frozen=True)
class ReplyReading:
    """How the latest reply reads, with the sender's trailing auto-reply streak (0 if not an auto-reply)."""

    intent: ReplyIntent
    cue: str | None
    auto_reply_streak: int = 0


def read_reply(messages: Sequence[str]) -> ReplyReading:
    """Read the last of ``messages``: the sender's inbound texts, oldest first, latest last."""
    if not messages:
        raise ValueError("messages must include the latest reply")
    n = VERBATIM_AUTO_REPLY_COUNT
    readings = [classify_text(m) for m in messages]
    texts = [normalize(m) for m in messages]
    auto = [intent is R.AUTO_REPLY for intent, _ in readings]
    for i in range(n - 1, len(messages)):
        if readings[i][0] in VERBATIM_ELIGIBLE and len(set(texts[i - n + 1 : i + 1])) == 1:
            auto[i - n + 1 : i + 1] = [True] * n

    intent, cue = readings[-1]
    if not auto[-1]:
        return ReplyReading(intent, cue)
    streak = len(auto) - next((i for i in range(len(auto) - 1, -1, -1) if not auto[i]), -1) - 1
    return ReplyReading(R.AUTO_REPLY, cue if intent is R.AUTO_REPLY else f"same message {n}x verbatim", streak)


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MerchantSuppression:
    """A merchant-wide suppression the decision requires (read by Phase 2C rule ``merchant_suppressed``)."""

    key: str
    reason: str


@dataclass(frozen=True)
class ReplyDecision:
    """The response plus the mutations it implies. ``reading`` is ``None`` when the reply was not recorded."""

    response: SendReply | WaitReply | EndReply
    state: ConversationState
    reading: ReplyReading | None
    suppression: MerchantSuppression | None = None


SEND_PLANS: dict[ReplyIntent, tuple[str, CtaType]] = {
    R.AFFIRMATIVE: ("action: confirm and proceed with the next step the {who} agreed to", CtaType.BINARY_CONFIRM_CANCEL),
    R.QUESTION: ("answer: reply from the recorded contexts only, then restate the pending next step", CtaType.OPEN_ENDED),
    R.OBJECTION: ("objection: acknowledge the concern and restate the grounded value of the proposal", CtaType.OPEN_ENDED),
    R.OFF_TOPIC: ("off_topic: politely decline the unrelated request and redirect to the original trigger", CtaType.OPEN_ENDED),
    R.UNCLEAR: ("clarify: restate the pending proposal as a single yes/no choice", CtaType.BINARY_YES_NO),
    R.AUTO_REPLY: ("auto_reply: flag the canned reply and ask the owner for a one-word YES to continue", CtaType.BINARY_YES_NO),
}


def decide_reply(conversation: Conversation, reading: ReplyReading, request: ReplyRequest) -> ReplyDecision:
    """The next step for ``conversation`` (inbound turn already appended). Pure."""
    current = conversation.state
    if current.is_terminal:
        return ReplyDecision(EndReply(rationale=CLOSED_RATIONALE), current, reading)

    who = request.from_role.value
    heard = f"{who.capitalize()} reply read as {reading.intent}" + (f" (cue: {reading.cue!r})" if reading.cue else "")

    def send(state: ConversationState, note: str) -> ReplyDecision:
        instruction, cta = SEND_PLANS[reading.intent]
        body = (
            f"{REPLY_PLACEHOLDER_PREFIX} {instruction.format(who=who)}"
            f" | trigger={conversation.trigger_id or 'none'} turn={request.turn_number}"
        )
        return ReplyDecision(SendReply(body=body, cta=cta, rationale=f"{heard}; {note}; state {current} -> {state}"), state, reading)

    def wait(seconds: int, state: ConversationState, note: str) -> ReplyDecision:
        rationale = f"{heard}; {note}; state {current} -> {state}"
        return ReplyDecision(WaitReply(wait_seconds=seconds, rationale=rationale), state, reading)

    def end(note: str, suppression: MerchantSuppression | None = None) -> ReplyDecision:
        rationale = f"{heard}; {note}; state {current} -> {S.ENDED}"
        return ReplyDecision(EndReply(rationale=rationale), S.ENDED, reading, suppression)

    engaged = S.COMMITTED if current is S.COMMITTED else S.QUALIFYING
    match reading.intent:
        case R.HOSTILE:
            if request.from_role is not FromRole.MERCHANT:
                return end("closing conversation; merchant-wide suppression applies to merchant replies only")
            if conversation.merchant_id is None:
                return end("closing conversation; merchant unknown, no merchant-wide suppression written")
            key = merchant_suppression_key(conversation.merchant_id)
            reason = f"hostile reply conversation={conversation.conversation_id}"
            note = f"closing conversation; suppressing all triggers for this merchant ({key}, no expiry)"
            return end(note, MerchantSuppression(key, reason))
        case R.OPT_OUT | R.NOT_INTERESTED:
            return end("gracefully exiting; no further messages on this conversation_id")
        case R.AUTO_REPLY if reading.auto_reply_streak >= AUTO_REPLY_END_STREAK:
            return end(f"auto-reply {reading.auto_reply_streak}x in a row, no real reply; closing")
        case R.AUTO_REPLY if reading.auto_reply_streak == AUTO_REPLY_END_STREAK - 1:
            return wait(AUTO_REPLY_WAIT_SECONDS, S.WAITING, "auto-reply 2x in a row; backing off 24h")
        case R.AUTO_REPLY:
            return send(current, "auto-reply is neither interest nor rejection; one prompt for the owner")
        case R.DEFERRAL:
            return wait(DEFER_WAIT_SECONDS, S.WAITING, "asked for time; backing off 30 min")
        case R.AFFIRMATIVE:
            return send(S.COMMITTED, "explicit commitment; switching to action mode")
        case R.OFF_TOPIC:
            return send(engaged, "staying on the original trigger")
        case _:
            return send(engaged, "engaged reply; continuing without re-qualifying")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def handle_reply(state: StateContainer, request: ReplyRequest) -> ReplyDecision:
    """Record the reply, decide the next step and apply its mutations."""
    with state.tick_lock:
        store = state.conversation_store
        conversation, created = store.get_or_create(
            request.conversation_id, merchant_id=request.merchant_id, customer_id=request.customer_id
        )
        if not created:
            conflict = ownership_conflict(conversation, request)
            if conflict is not None:
                logger.warning("reply rejected conversation_id=%s %s", request.conversation_id, conflict)
                rationale = f"Reply does not belong to this conversation ({conflict}); not recorded."
                return ReplyDecision(EndReply(rationale=rationale), conversation.state, None)
            conversation = _backfill(store, conversation, request)

        prior = sender_messages(store, conversation, request.from_role)
        conversation = store.append_message(
            request.conversation_id,
            role=TurnRole(request.from_role.value),
            body=request.message,
            sent_at=request.received_at,
            turn_number=request.turn_number,
        )
        reading = read_reply([*prior, request.message])
        decision = realize(decide_reply(conversation, reading, request), conversation, state.context_store)
        _apply(state, conversation, decision, request)

    logger.info(
        "reply decided conversation_id=%s from_role=%s turn_number=%d message_chars=%d intent=%s "
        "auto_reply_streak=%d action=%s state=%s->%s",
        request.conversation_id, request.from_role, request.turn_number, len(request.message), reading.intent,
        reading.auto_reply_streak, decision.response.action, conversation.state, decision.state,
    )
    return decision


def ownership_conflict(conversation: Conversation, request: ReplyRequest) -> str | None:
    """Why the reply cannot belong to ``conversation``, or ``None``.

    A conversation whose merchant is still unknown (opened by a reply without
    ids) accepts ids later; otherwise a conflicting merchant or customer id is
    refused rather than re-assigning the thread.
    """
    for field in ("customer_id", "merchant_id"):
        stored, incoming = getattr(conversation, field), getattr(request, field)
        if incoming is not None and stored != incoming and (stored is not None or conversation.merchant_id is not None):
            return f"{field}={incoming}; conversation {field}={stored}"
    return None


def sender_messages(store: ConversationStore, conversation: Conversation, from_role: FromRole) -> list[str]:
    """The sender's earlier inbound texts in arrival order: across their conversations when the merchant is known."""
    threads = (
        store.find(merchant_id=conversation.merchant_id, customer_id=conversation.customer_id)
        if conversation.merchant_id is not None
        else [conversation]
    )
    role = TurnRole(from_role.value)
    turns = [
        (turn.recorded_at, turn.sent_at, thread.conversation_id, index, turn.body)
        for thread in threads
        for index, turn in enumerate(thread.turns)
        if turn.role is role
    ]
    return [body for *_, body in sorted(turns)]


def _backfill(store: ConversationStore, conversation: Conversation, request: ReplyRequest) -> Conversation:
    """Fill in participant ids a conversation opened without them."""
    if conversation.merchant_id is not None or request.merchant_id is None:
        return conversation
    customer_id = request.customer_id if conversation.customer_id is None else None
    return store.update(conversation.conversation_id, merchant_id=request.merchant_id, customer_id=customer_id)


def _apply(state: StateContainer, conversation: Conversation, decision: ReplyDecision, request: ReplyRequest) -> None:
    """State transition, merchant suppression (an existing indefinite one is kept), then the Vera turn for a send."""
    if decision.state is not conversation.state:
        state.conversation_store.set_state(conversation.conversation_id, decision.state)
    if decision.suppression is not None:
        write = decision.suppression
        existing = state.suppression_store.peek(write.key, request.received_at)
        if existing is None or existing.expires_at is not None:
            state.suppression_store.suppress(write.key, reason=write.reason)
    if isinstance(decision.response, SendReply):
        state.conversation_store.append_message(
            conversation.conversation_id, role=TurnRole.VERA, body=decision.response.body, sent_at=request.received_at
        )


__all__ = [
    "AUTO_REPLY_END_STREAK",
    "AUTO_REPLY_WAIT_SECONDS",
    "CLOSED_RATIONALE",
    "DEFER_WAIT_SECONDS",
    "REPLY_PLACEHOLDER_PREFIX",
    "VERBATIM_AUTO_REPLY_COUNT",
    "MerchantSuppression",
    "ReplyDecision",
    "ReplyIntent",
    "ReplyReading",
    "classify_text",
    "decide_reply",
    "handle_reply",
    "normalize",
    "ownership_conflict",
    "read_reply",
    "sender_messages",
]
