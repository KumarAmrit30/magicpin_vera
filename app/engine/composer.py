"""Phase 3 message composer: a selected DecisionPlan -> the WhatsApp message that realizes it.

    DecisionPlan (+ its CandidateGenerationContext)
        -> grounded facts: the plan's evidence, re-checked against the context
        -> salutation / lead facts / citation / proposal / one CTA
        -> ComposedMessage (body, wire cta, send_as, template_name, template_params)

The composer never chooses, ranks or changes a decision. Every value in a body
comes from the plan's evidence or from context identity fields (owner name,
business name, customer name, category salutation, digest source); fixed
template text adds no facts. Deterministic: no clock, randomness or I/O.

Contract sources: challenge-brief.md §5 (single primary CTA, don't fabricate,
voice match, send_as), §10-11 and examples/case-studies.md (specificity,
owner name, source citation, CTA in the last sentence), challenge-testing-brief.md
§2.2 (template_name / positional template_params).

``merchant_on_behalf`` messages are the customer-facing message itself, sent
from the merchant's number (case studies 2, 8, 10): the business speaks, never
Vera, and no merchant-internal fact is used.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.engine.actions import ActionType, CTAType, DecisionScope
from app.engine.archetypes import TriggerArchetype
from app.engine.candidates import CandidateGenerationContext
from app.engine.evidence import Evidence, EvidenceSource, is_grounded
from app.engine.plans import DecisionPlan
from app.models.enums import CtaType, SendAs

A = ActionType

TEMPLATE_VERSION = "v1"

WIRE_CTA: dict[CTAType, CtaType] = {
    CTAType.NONE: CtaType.NONE,
    CTAType.YES_NO: CtaType.BINARY_YES_NO,
    CTAType.OPEN_ENDED: CtaType.OPEN_ENDED,
    CTAType.CONFIRMATION: CtaType.BINARY_CONFIRM_CANCEL,
}
"""One-to-one planning -> wire CTA mapping (the wire values of challenge-testing-brief.md §2)."""

LEAD_BUDGET_CHARS = 320
MAX_LEAD_FACTS = 4


class CompositionError(ValueError):
    """The plan cannot be realized as a message (e.g. ``no_action``)."""


@dataclass(frozen=True, slots=True)
class ComposedMessage:
    """A realized message. ``template`` is ``body`` with ``{{n}}`` in place of each ``template_params`` entry."""

    body: str
    cta: CtaType
    send_as: SendAs
    template_name: str
    template_params: tuple[str, ...]
    template: str
    facts_used: tuple[str, ...]
    """``source:field`` of every context value the body renders."""


# --------------------------------------------------------------------------- #
# Value rendering (formatting only; never adds facts)
# --------------------------------------------------------------------------- #

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2}|Z)?$")


def humanize(text: str) -> str:
    """ISO dates -> ``15 Dec 2026``; single snake_case tokens -> words; anything else unchanged."""
    if ISO_DATE.match(text):
        day = datetime.fromisoformat(text)
        return f"{day.day} {day:%b %Y}"
    if ISO_DATETIME.match(text):
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        date = f"{moment.day} {moment:%b %Y}"
        if (moment.hour, moment.minute) == (0, 0):
            return date
        hour = moment.hour % 12 or 12
        minutes = f":{moment.minute:02d}" if moment.minute else ""
        return f"{date}, {hour}{minutes}{'am' if moment.hour < 12 else 'pm'}"
    if " " not in text and "_" in text:
        return text.replace("_", " ")
    return text


def join_words(items: Sequence[str], conjunction: str = "and") -> str:
    items = list(items)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


def display(evidence: Evidence) -> str:
    """The recipient-facing rendering of an evidence value."""
    value = evidence.value
    if isinstance(value, str):
        return humanize(value)
    if isinstance(value, list) and value and all(isinstance(item, Mapping) for item in value):
        labels = [str(item.get("label") or item.get("title") or item.get("iso")) for item in value]
        return join_words([humanize(label) for label in labels], "or")
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return join_words([humanize(item) for item in value])
    return evidence.formatted.partition(": ")[2]


def label_of(evidence: Evidence) -> str:
    return evidence.formatted.partition(": ")[0]


# --------------------------------------------------------------------------- #
# Merchant-facing fact phrases, keyed by Phase 2B evidence label
# --------------------------------------------------------------------------- #

HIDDEN_LABELS = frozenset({
    "trigger kind", "merchant signal", "matching cohort", "relevant segment", "merchant category",
    "relevant categories", "customer state", "preferred slots", "sentiment", "question theme", "merchant said",
    "merchant asked", "Vera suggested", "Vera offered", "unanswered Vera message",
    "days since the merchant last replied", "imminent", "shelf action recommended", "last topic", "window", "baseline",
})
"""Internal codes, conversation quotes (anti-repetition) and routing hints: never rendered."""

MERCHANT_PHRASES: dict[str, str] = {
    "research": "New research: {}.",
    "regulation": "Regulation update: {}.",
    "finding": "{}",
    "what changes": "{}",
    "alert details": "{}",
    "category evidence": "{}",
    "category digest": "{}",
    "category detail": "{}",
    "category alert": "{}",
    "suggested step": "Suggested step: {}.",
    "required step": "Required step: {}.",
    "compliance deadline": "Deadline: {}.",
    "issued by": "Source: {}.",
    "organiser": "Organiser: {}.",
    "source": "Source: {}.",
    "matching customers": "You have {} matching {noun} on record.",
    "chronic-Rx customers": "You have {} chronic-Rx {noun} on record.",
    "active members": "You have {} active members.",
    "affected batches": "Affected batches: {}.",
    "recalled molecule": "Recalled molecule: {}.",
    "manufacturer": "Manufacturer: {}.",
    "days until renewal": "Your subscription is due for renewal in {} days.",
    "plan": "Plan: {}.",
    "renewal amount": "Renewal amount: {}.",
    "subscription status": "Subscription status: {}.",
    "days since expiry": "Your subscription lapsed {} days ago.",
    "customers lapsed since expiry": "{} customers have lapsed since the subscription expired.",
    "festival": "{} is coming up.",
    "festival date": "Date: {}.",
    "days until festival": "That's {} days away.",
    "match": "Match: {}.",
    "match time": "Start time: {}.",
    "venue": "Venue: {}.",
    "seasonal pattern": "Seasonal pattern: {}.",
    "season": "Season: {}.",
    "active offer": "Your active offer: {}.",
    "their offer": "Their offer: {}.",
    "delivery orders (30d)": "You've had {} delivery orders in the last 30 days.",
    "calls (30d)": "You've had {} calls in the last 30 days.",
    "views (30d)": "Your listing had {} views in the last 30 days.",
    "review theme": "A theme is emerging in your reviews: {}.",
    "customer quote": "One customer wrote: \u201c{}\u201d.",
    "mentions (30d)": "It came up {} times in the last 30 days.",
    "trend": "Trend: {}.",
    "milestone": "Milestone: {}.",
    "review_count now": "You're now at {} reviews.",
    "merchant's plan": "You mentioned planning: {}.",
    "demand trends": "Demand trends: {}.",
    "estimated uplift": "Estimated uplift once verified: {}.",
    "verification path": "Verification is by {}.",
    "session": "Upcoming session: {}.",
    "session date": "Date: {}.",
    "credits": "Credits: {}.",
    "fee": "Fee: {}.",
    "competitor": "New competitor nearby: {}.",
    "distance (km)": "Distance: {} km.",
    "opened": "Opened: {}.",
    "pos review theme": "Your reviews praise: {}.",
    "likely driver": "Likely driver: {}.",
    "monthly churn": "Monthly churn: {}.",
    "trial size": "Trial size: {}.",
}

BOOLEAN_PHRASES: dict[str, dict[str, str]] = {
    "expected seasonal dip": {"yes": "This dip matches the expected seasonal pattern."},
    "weeknight match": {"yes": "It's a weeknight match.", "no": "It's not a weeknight match."},
    "listing verified": {"no": "Your listing isn't verified yet."},
}

CHANGE_LABEL = re.compile(r"^(calls|views)(?:_pct)?(?: change)?(?: \((\d+)d\))?$")

LISTING_GAPS: dict[str, Callable[[re.Match[str]], str]] = {
    r"unverified_gbp": lambda m: "verifying your Google profile",
    r"no_active_offers": lambda m: "adding an active offer",
    r"stale_posts:(\d+)d": lambda m: f"posting again (last post {m.group(1)} days ago)",
    r"ctr_below_peer_median": lambda m: "lifting click-through (below the peer median)",
}

CUSTOMER_NOUNS = {"dentists": "patients", "gyms": "members", "salons": "clients"}
"""Category vocabulary for the merchant's customers; ``customers`` otherwise."""


def listing_gap(code: str) -> str | None:
    for pattern, phrase in LISTING_GAPS.items():
        match = re.fullmatch(pattern, code)
        if match:
            return phrase(match)
    return None


def change_sentence(label: str, text: str) -> tuple[str, str] | None:
    """``calls change (7d): -30%`` -> ``Your calls are down {} over the last 7 days.`` with ``30%``."""
    match = CHANGE_LABEL.match(label)
    subject = f"Your {match.group(1)}" if match else "Performance" if label == "performance change since expiry" else None
    if subject is None or not re.fullmatch(r"[+-]?\d+(\.\d+)?%", text):
        return None
    verb = "are" if match else "is"
    window = f" over the last {match.group(2)} days" if match and match.group(2) else ""
    if label == "performance change since expiry":
        window = " since the subscription expired"
    magnitude = text.lstrip("+-")
    if float(magnitude.rstrip("%")) == 0:
        return f"{subject} {verb} unchanged{window}.", ""
    direction = "down" if text.startswith("-") else "up"
    return f"{subject} {verb} {direction} {{}}{window}.", magnitude


# --------------------------------------------------------------------------- #
# Body builder: records each inserted value as a template parameter
# --------------------------------------------------------------------------- #


@dataclass
class TemplateWriter:
    body: list[str] = field(default_factory=list)
    template: list[str] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)

    def render(self, pattern: str, values: Sequence[str]) -> tuple[str, str]:
        pieces = pattern.split("{}")
        if len(pieces) != len(values) + 1:
            raise CompositionError(f"pattern {pattern!r} expects {len(pieces) - 1} values")
        body, template = pieces[0], pieces[0]
        for offset, (value, piece) in enumerate(zip(values, pieces[1:], strict=True)):
            body += value + piece
            template += f"{{{{{len(self.params) + offset + 1}}}}}" + piece
        return body, template

    def add(self, pattern: str, *values: str, facts: Sequence[str] = ()) -> None:
        body, template = self.render(pattern, values)
        self.body.append(body)
        self.template.append(template)
        self.params.extend(values)
        self.facts.extend(f for f in facts if f not in self.facts)

    @property
    def text(self) -> str:
        return " ".join(self.body)


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?", "\u201d.")) else f"{text}."


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Fact:
    evidence: Evidence
    label: str
    text: str

    @property
    def ref(self) -> str:
        return f"{self.evidence.source}:{self.evidence.field}"


def compose(plan: DecisionPlan, context: CandidateGenerationContext) -> ComposedMessage:
    """Realize ``plan`` as one message. Raises :class:`CompositionError` for ``no_action`` or a mismatched context."""
    if plan.is_no_action:
        raise CompositionError(f"no_action plan {plan.plan_id} has no message")
    if (plan.trigger_id, plan.merchant_id, plan.customer_id) != (context.trigger_id, context.merchant_id, context.customer_id):
        raise CompositionError(f"context does not belong to plan {plan.plan_id}")

    facts = _grounded_facts(plan, context)
    taboos = [t.lower() for t in context.category.get("voice", {}).get("vocab_taboo") or [] if isinstance(t, str)]
    writer = TemplateWriter()
    if plan.scope is DecisionScope.CUSTOMER:
        _customer_message(writer, plan, context, facts, taboos)
    else:
        _merchant_message(writer, plan, context, facts, taboos)

    return ComposedMessage(
        body=writer.text,
        cta=WIRE_CTA[plan.cta_type],
        send_as=plan.send_as,
        template_name=f"vera_{plan.action.value}_{TEMPLATE_VERSION}",
        template_params=tuple(writer.params),
        template=" ".join(writer.template),
        facts_used=tuple(writer.facts),
    )


def _grounded_facts(plan: DecisionPlan, context: CandidateGenerationContext) -> list[_Fact]:
    """The plan's evidence, most important first, keeping only values still present in the context."""
    ordered = sorted(plan.evidence, key=lambda e: -e.importance)
    return [
        _Fact(e, label_of(e), display(e))
        for e in ordered
        if is_grounded(e, context.source_data(e.source)) and label_of(e) not in HIDDEN_LABELS
    ]


def _allowed(text: str, taboos: Sequence[str]) -> bool:
    lowered = text.lower()
    return "?" not in text and not any(t in lowered for t in taboos)


# ------------------------------- merchant --------------------------------- #


REDUNDANT_WITH = {"subscription status": {"days since expiry", "days until renewal"}}
"""A label is dropped from the lead when one of these labels is also present (the other says it better)."""


def _merchant_message(
    writer: TemplateWriter, plan: DecisionPlan, context: CandidateGenerationContext, facts: list[_Fact], taboos: list[str]
) -> None:
    noun = CUSTOMER_NOUNS.get(context.category.get("slug"), "customers")
    labels = {f.label for f in facts}
    salutation = _salutation(context)

    def add(pattern: str, values: Sequence[str], refs: Sequence[str]) -> None:
        nonlocal salutation
        if salutation is not None:
            (greeting, *names), name_refs = salutation
            pattern, values, refs = f"{greeting} \u2014 {pattern}", (*names, *values), (*name_refs, *refs)
            salutation = None
        writer.add(pattern, *values, facts=refs)

    rendered = 0
    for fact in facts:
        if labels & REDUNDANT_WITH.get(fact.label, set()):
            continue
        if fact.label == "listing gap" and plan.action is A.RECOMMEND_OPERATIONAL_FIX:
            continue
        phrase = _merchant_phrase(fact, noun)
        if phrase is None:
            continue
        pattern, values = phrase
        body, _ = writer.render(pattern, values)
        if not _allowed(body, taboos) or any(body in b for b in writer.body):
            continue
        if rendered and len(writer.text) + len(body) > LEAD_BUDGET_CHARS:
            continue
        add(pattern, values, (fact.ref,))
        rendered += 1
        if rendered == MAX_LEAD_FACTS:
            break

    _citation(writer, context, taboos)
    proposal = _proposal(plan, facts, noun)
    if proposal is not None:
        pattern, values, refs = proposal
        if _allowed(writer.render(pattern, values)[0], taboos):
            add(pattern, values, refs)
    cta = _merchant_cta(plan, facts)
    if cta is not None:
        add(*cta)
    if not writer.body:
        add("A quick update for you.", (), ())


def _salutation(context: CandidateGenerationContext) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    return merchant_salutation(context.merchant, context.category)


def merchant_salutation(merchant: Mapping[str, Any], category: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """The category's first salutation example that the merchant identity fills, e.g. ``Dr. {first_name}``.

    Returns ``((pattern, *values), refs)``; ``None`` when no example can be filled.
    """
    identity = merchant.get("identity") or {}
    examples = (category.get("voice") or {}).get("salutation_examples") or []
    for index, example in enumerate(examples):
        if not isinstance(example, str):
            continue
        slots = re.findall(r"\{(\w+)\}", example)
        if not slots:
            continue
        fields = ["owner_first_name" if slot.endswith(("first_name", "pharmacist_name")) else "name" for slot in slots]
        values = [identity.get(f) for f in fields]
        if all(isinstance(v, str) and v for v in values):
            refs = (f"category:voice.salutation_examples.{index}", *(f"merchant:identity.{f}" for f in fields))
            return (re.sub(r"\{\w+\}", "{}", example), *values), refs
    return None


def _merchant_phrase(fact: _Fact, noun: str) -> tuple[str, tuple[str, ...]] | None:
    label, text = fact.label, fact.text
    if label in BOOLEAN_PHRASES:
        sentence = BOOLEAN_PHRASES[label].get(text)
        return (sentence, ()) if sentence else None
    change = change_sentence(label, text)
    if change is not None:
        pattern, value = change
        return (pattern, (value,)) if value else (pattern, ())
    if label == "listing gap":
        phrase = listing_gap(str(fact.evidence.value))
        return ("Worth fixing: {}.", (phrase,)) if phrase else None
    if label == "subscription status":
        return ("Your subscription is {}.", (text,))
    pattern = MERCHANT_PHRASES.get(label, f"{label[:1].upper()}{label[1:]}: {{}}.")
    if pattern == "{}":
        return ("{}" if text.endswith((".", "!")) else "{}."), (text,)
    return _sentence(pattern.replace("{noun}", noun)), (text,)


def _citation(writer: TemplateWriter, context: CandidateGenerationContext, taboos: list[str]) -> None:
    """Cite the digest item a rendered fact came from, when it names a source not already in the body."""
    for ref in list(writer.facts):
        match = re.fullmatch(r"category:digest\.(\d+)\.\w+", ref)
        if not match:
            continue
        path = f"digest.{match.group(1)}.source"
        source = context.value(EvidenceSource.CATEGORY, path)
        if isinstance(source, str) and source and source not in writer.text and _allowed(source, taboos):
            writer.add("Source: {}.", source, facts=(f"category:{path}",))
        return


def _find(facts: list[_Fact], *labels: str) -> _Fact | None:
    return next((f for f in facts if f.label in labels), None)


def _proposal(plan: DecisionPlan, facts: list[_Fact], noun: str) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    """What Vera offers to do; an offer, never a claim that anything was done."""
    action = plan.action
    if action is A.DRAFT_CAMPAIGN:
        offer = _find(facts, "active offer")
        if offer:
            return "I can draft a campaign around your \u201c{}\u201d offer.", (offer.text,), (offer.ref,)
        return "I can draft a campaign for it.", (), ()
    if action is A.DRAFT_LISTING:
        praise = _find(facts, "pos review theme")
        if praise:
            return "I can draft a listing update that leads with what customers praise ({}).", (praise.text,), (praise.ref,)
        return "I can draft a listing update for it.", (), ()
    if action is A.DRAFT_POST:
        driver = _find(facts, "likely driver")
        if driver:
            return "I can draft a post in the same vein as your {}.", (driver.text,), (driver.ref,)
        return "I can draft a post for it.", (), ()
    if action is A.DRAFT_MESSAGE:
        if _find(facts, "review theme", "customer quote"):
            return "I can draft replies to those reviews for you to approve.", (), ()
        if plan.archetype is TriggerArchetype.SAFETY_COMPLIANCE:
            return f"I can draft the note to affected {noun} for you to review.", (), ()
        return "I can draft the message for you to review.", (), ()
    if action is A.DRAFT_ARTIFACT:
        topic = _find(facts, "merchant's plan")
        if topic:
            return "I can draft a starter version of the {} for you to edit.", (topic.text,), (topic.ref,)
        if any(f.evidence.source is EvidenceSource.CATEGORY for f in facts):
            return f"I can draft a short explainer you can share with your {noun}.", (), ()
        return "I can draft it for you to review.", (), ()
    if action is A.RECOMMEND_RETENTION:
        members = _find(facts, "active members")
        if members:
            return f"Suggestion: focus on retaining your {{}} active {noun} for now.", (members.text,), (members.ref,)
        return f"Suggestion: focus on retaining your existing {noun} for now.", (), ()
    if action is A.RECOMMEND_OPERATIONAL_FIX:
        verified = _find(facts, "listing verified")
        if verified is not None and verified.text == "no":
            return "Getting the listing verified is the first fix.", (), ()
        gaps = [(g, f.ref) for f in facts if f.label == "listing gap" and (g := listing_gap(str(f.evidence.value)))]
        if gaps:
            return "The fixes I'd start with: {}.", (join_words([g for g, _ in gaps]),), tuple(r for _, r in gaps)
        if any(f.evidence.field == "payload.shelf_action_recommended" and f.evidence.value is True for f in plan_facts(plan)):
            return "A stock and shelf update is recommended for this.", (), ("trigger:payload.shelf_action_recommended",)
        return None
    return None


def plan_facts(plan: DecisionPlan) -> list[_Fact]:
    """All plan evidence as facts, hidden labels included (for proposals that act on routing hints)."""
    return [_Fact(e, label_of(e), display(e)) for e in plan.evidence]


ASK_QUESTIONS = {"what_service_in_demand_this_week": "What service have customers asked for most this week?"}
"""Known ``payload.ask_template`` values; unknown ones fall back to a generic open question."""


def _merchant_cta(plan: DecisionPlan, facts: list[_Fact]) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    cta, action = plan.cta_type, plan.action
    drafting = action in {A.DRAFT_CAMPAIGN, A.DRAFT_LISTING, A.DRAFT_POST, A.DRAFT_MESSAGE, A.DRAFT_ARTIFACT}
    if cta is CTAType.NONE:
        return None
    if cta is CTAType.CONFIRMATION:
        return ("Reply CONFIRM and I'll draft it." if drafting else "Reply CONFIRM to go ahead."), (), ()
    if cta is CTAType.OPEN_ENDED:
        if action is A.ASK_MERCHANT:
            return _ask_question(plan, facts)
        return "What do you think?", (), ()
    if drafting:
        return "Want me to draft it?", (), ()
    return {
        A.SEND_INSIGHT: "Want the full details?",
        A.SEND_ALERT: "Want me to share the next steps?",
        A.RECOMMEND_RETENTION: "Want me to draft a retention plan?",
        A.RECOMMEND_OPERATIONAL_FIX: "Want me to walk you through it?",
    }.get(action, "Want to take this forward?"), (), ()


def _ask_question(plan: DecisionPlan, facts: list[_Fact]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    festival = _find(facts, "festival")
    if festival:
        return "What are you planning for {}?", (festival.text,), (festival.ref,)
    theme = next((f for f in plan_facts(plan) if f.label == "question theme"), None)
    if theme and str(theme.evidence.value) in ASK_QUESTIONS:
        return ASK_QUESTIONS[str(theme.evidence.value)], (), (theme.ref,)
    return "What would be most useful for you right now?", (), ()


# ------------------------------- customer --------------------------------- #

CUSTOMER_LABELS = frozenset({
    "due date", "service due", "last service", "last visit", "days since last visit", "previous focus",
    "months as a member", "trial date", "trial completed", "next step", "wedding date", "days to wedding",
    "medicines", "stock runs out", "delivery address saved", "last refill", "active offer", "available options",
})
"""The only facts a customer-facing message may use; everything else is merchant-internal."""

HINDI_PREFS = ("hi", "hi-en mix")


def _customer_message(
    writer: TemplateWriter, plan: DecisionPlan, context: CandidateGenerationContext, facts: list[_Fact], taboos: list[str]
) -> None:
    usable = {}
    for fact in facts:
        if fact.label in CUSTOMER_LABELS and fact.label not in usable and _allowed(fact.text, taboos):
            usable[fact.label] = fact

    def add(pattern: str, *labels: str) -> None:
        writer.add(pattern, *(usable[label].text for label in labels), facts=tuple(usable[label].ref for label in labels))

    greeting, greeting_values, greeting_refs, possessive = _customer_greeting(context)
    writer.add(greeting, *greeting_values, facts=greeting_refs)
    has = usable.__contains__
    first_option = _first_option(usable.get("available options"))

    if plan.action is A.SEND_CUSTOMER_REMINDER:
        if has("service due"):
            add(f"{possessive} {{}} is due on {{}}." if has("due date") else f"{possessive} {{}} is due.",
                *(("service due", "due date") if has("due date") else ("service due",)))
            if has("last service"):
                add("The last one was on {}.", "last service")
        elif has("medicines"):
            if has("stock runs out"):
                add(f"{possessive} medicines ({{}}) run out on {{}}.", "medicines", "stock runs out")
            else:
                add(f"{possessive} medicines ({{}}) are due for a refill.", "medicines")
            if has("last refill"):
                add("Last refill: {}.", "last refill")
        elif has("due date"):
            add(f"{possessive} next visit is due on {{}}.", "due date")
    elif plan.action is A.SEND_CUSTOMER_WINBACK:
        if has("days since last visit"):
            add("It's been {} days since your last visit \u2014 no pressure at all.", "days since last visit")
        elif has("last visit"):
            add("It's been a while since your last visit on {} \u2014 no pressure at all.", "last visit")
        if has("previous focus"):
            add("We're here whenever you want to get back to your {} goals.", "previous focus")
    else:
        if has("trial date"):
            add("Following up on your trial on {}.", "trial date")
        elif has("trial completed"):
            add("Following up on your trial on {}.", "trial completed")
        if has("days to wedding") and has("wedding date"):
            add("{} days to go until your wedding on {}.", "days to wedding", "wedding date")
        elif has("wedding date"):
            add("Your wedding is on {}.", "wedding date")
        if has("next step"):
            add("Next step: {}.", "next step")

    if len(writer.body) == 1 and has("last visit"):
        add("Your last visit with us was on {}.", "last visit")
    if has("active offer"):
        add("Current offer: {}.", "active offer")
    if has("available options"):
        add("Open slots: {}.", "available options")
    if has("delivery address saved") and usable["delivery address saved"].text == "yes":
        writer.add("Your delivery address is saved with us.", facts=(usable["delivery address saved"].ref,))

    refill = plan.action is A.SEND_CUSTOMER_REMINDER and has("medicines")
    if plan.cta_type is CTAType.CONFIRMATION:
        if refill:
            writer.add("Reply CONFIRM to arrange your refill.")
        elif first_option:
            writer.add("Reply CONFIRM to book {}.", first_option, facts=(usable["available options"].ref,))
        else:
            writer.add("Reply CONFIRM to book your next visit.")
    elif plan.cta_type is CTAType.YES_NO:
        if refill:
            writer.add("Shall we arrange your refill?")
        elif first_option:
            writer.add("Shall we book {} for you?", first_option, facts=(usable["available options"].ref,))
        elif plan.action is A.SEND_CUSTOMER_WINBACK:
            writer.add("Want to book a session?")
        else:
            writer.add("Want us to book your next visit?")
    elif plan.cta_type is CTAType.OPEN_ENDED:
        writer.add("What time works best for you?")


def _customer_greeting(context: CandidateGenerationContext) -> tuple[str, tuple[str, ...], tuple[str, ...], str]:
    return customer_greeting(context.customer, context.merchant)


def customer_greeting(
    customer: Mapping[str, Any] | None, merchant: Mapping[str, Any]
) -> tuple[str, tuple[str, ...], tuple[str, ...], str]:
    """Greeting pattern, its values and refs, and the possessive for the person the message is about."""
    identity = (customer or {}).get("identity") or {}
    business = (merchant.get("identity") or {}).get("name")
    name = identity.get("name") if isinstance(identity.get("name"), str) else ""
    hello = "Namaste" if identity.get("language_pref") in HINDI_PREFS else "Hi"
    parent = re.fullmatch(r"\s*(.+?)\s*\(parent:\s*(.+?)\)\s*", name)
    refs: list[str] = []
    if parent:
        addressee, possessive = parent.group(2), f"{parent.group(1)}'s"
    elif name and not name.startswith("("):
        addressee, possessive = name, "Your"
    else:
        addressee, possessive = "", "Your"
    values: list[str] = []
    pattern = hello
    if addressee:
        pattern += " {}"
        values.append(addressee)
        refs.append("customer:identity.name")
    if isinstance(business, str) and business:
        pattern += ", {} here."
        values.append(business)
        refs.append("merchant:identity.name")
    else:
        pattern += "."
    return pattern, tuple(values), tuple(refs), possessive


def _first_option(fact: _Fact | None) -> str | None:
    if fact is None or not isinstance(fact.evidence.value, list) or not fact.evidence.value:
        return None
    first = fact.evidence.value[0]
    if isinstance(first, Mapping):
        label = first.get("label") or first.get("iso")
        return humanize(str(label)) if label else None
    return humanize(str(first)) if isinstance(first, str) else None


def reads_as(message: ComposedMessage) -> str:
    """``template`` with parameters substituted; equals ``body`` for every composed message."""
    text = message.template
    for index, value in reversed(list(enumerate(message.template_params, start=1))):
        text = text.replace(f"{{{{{index}}}}}", value)
    return text


__all__ = [
    "TEMPLATE_VERSION",
    "WIRE_CTA",
    "ComposedMessage",
    "CompositionError",
    "TemplateWriter",
    "compose",
    "customer_greeting",
    "humanize",
    "merchant_salutation",
    "reads_as",
]
