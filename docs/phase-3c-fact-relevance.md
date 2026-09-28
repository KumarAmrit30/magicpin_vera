# Phase 3C — Question Relevance

## 1. Purpose

Phase 3B answered every question with the tick opener's first fact, so "How
much is the cleaning?" got the due date. Phase 3C picks the grounded fact that
answers what was actually asked. It is wording only:

```text
Phase 2F → WHAT Vera does (send / wait / end, CTA, state, suppression)   unchanged
Phase 3B/3C → HOW a send is worded                                       this layer
```

Code: `app/engine/reply_facts.py` (relevance), used by
`app/engine/reply_composer.py` (wording). No other module changed.

## 2. Fact model

No new facts are introduced. A fact is one of:

| Source | Used for | Ref |
|---|---|---|
| A sentence of the tick opener (already grounded and audience-filtered by the Phase 3A composer) | merchant and customer conversations | `turn:0` |
| A customer-facing context field, read verbatim | customer conversations only | `<scope>:<path>` |

Customer context fields: `available_slots` / `next_session_options` labels,
`appointment_iso`, `due_date`, `stock_runs_out_iso`, `last_service_date` or
`relationship.last_visit`, and active merchant offers in price-point form
(`Dental Cleaning @ ₹299`; thresholds like `Free Home Delivery > ₹499` are not
prices). Dates go through the Phase 3 `humanize`. Category taboos are applied.
Merchant conversations never draw on context fields, so merchant-internal data
is never offered to a customer and a merchant is never quoted its own offer as
an answer.

Opener sentences are tagged by the fixed Phase 3A phrasings they use
(`SENTENCE_KINDS`: `Open slots:`, `… is due on …`, `Fee:`, `Source:`, `up 12%`,
`124 lapsed patients`, …). Offers, CTAs, questions and markup are excluded.

## 3. Pipeline

```text
inbound question
  → requests  (REQUESTS: phrase patterns → wanted fact kinds; at most 2)
  → pool      (opener facts; customer context facts first for customer voice)
  → rank      (per request)
  → Answer(facts, missing, fallbacks, part_of_day, matching_slots)
  → compose   (fact lines · missing lines · offer · one question)
```

| Request | Example phrases | Fact kinds |
|---|---|---|
| price | how much, cost, price, fee, charge, rate, kitna, ₹ | PRICE |
| availability | slot, available, timing, what time, when can I come, morning/afternoon/evening/weekend | AVAILABILITY |
| appointment | appointment, booking, booked | APPOINTMENT (fallback: AVAILABILITY) |
| due | due, runs out, renewal | DUE_DATE |
| deadline | deadline, by when, last date | DEADLINE, DUE_DATE |
| last_visit | last visit/cleaning/refill, when did I | LAST_VISIT |
| count | how many, number of | COUNT |
| change | by how much, how bad, percent, % | CHANGE |
| source | source, where is this from, which study | SOURCE |
| date (generic) | when, what date | EVENT_DATE, DUE_DATE, DEADLINE, APPOINTMENT |

Overlapping matches keep the earliest, then longest, then first declared. The
generic `date` request is dropped when a specific one is present ("when is it
due" → due).

## 4. Ranking

For each request, candidate facts of the wanted kinds are ordered by:

1. word overlap between the fact and the question's remaining content words
   ("the cleaning" favours the cleaning offer);
2. mentioned in the latest Vera message;
3. mentioned in any earlier Vera message;
4. structured value first for price and availability, the opener's own
   sentence first for everything else;
5. the request's kind order, then pool order.

A price fact that shares no word with a named item ("How much is the bridal
package?" when only a haircut offer exists) is not used. A direct match always
wins over the opener's first fact; the first fact is never a fallback.

A part of day in the question filters the slot labels by the slot's ISO start
time (morning 5–12, afternoon 12–17, evening 17–23, weekend Sat/Sun).

## 5. Wording

The composer key is `why` for a why-question with a recorded reason, `answer`
for any other question with at least one request, and the Phase 2F intent
otherwise. `answer` bodies:

- fact lines, in the order asked: `Our current offer is Dental Cleaning @ ₹299.`,
  `Yes — we have Wed 5 Nov, 6pm or Thu 6 Nov, 5pm open.`, or the opener sentence;
- a part of day with no matching slot: `We don't have an evening slot open. The open slots are …`;
- missing facts are stated, never guessed: `We don't have the price on file.` /
  `I don't have that number on record.`, followed by a related fallback if one exists;
- the offer from Vera's latest message, skipped if the answer already contains it;
- one closing question (`Which slot works best for you?` when slots were given).

Questions with no recognised request ("Tell me more.", "What else?") keep the
Phase 2F classification. A general question gets the next opener fact not yet
repeated in later Vera turns (`What I have on record: …`), then the reason.
Voice rules are those of Phase 3B: customers hear "we" and never "Vera"; no ids,
scores, rationale or state appear in a body.

## 6. CONFIRM / CANCEL (Case B)

Phase 2F reads `CONFIRM`/`YES` as affirmative and `STOP` as opt-out (end), but
`Cancel` as unclear. The Phase 3B confirm prompt said `or CANCEL to stop.`,
advertising a word the reader does not honour.

Authoritative materials were checked: `CANCEL` appears only in the CTA enum
name `binary_confirm_cancel`; the api-call-examples (§4.2) and case-study
confirm prompts say only `Reply CONFIRM to …`; the challenge brief's binary
keywords are `YES` / `STOP`. No material defines CANCEL handling, so no
contract requires changing Phase 2F, and it was not changed.

The mismatch is fixed on the wording side: the prompt now reads
`Reply CONFIRM and I'll <offer>, or STOP to end here.`, and both keywords mean
what the prompt says (tested).

CANCEL remains an observed conversational limitation, but no authoritative
contract currently requires changing it.

## 7. Guarantees

- Phase 2F action, CTA, `wait_seconds`, rationale, state transition,
  suppression, auto-reply counting and classification are unchanged (tested
  across 4 triggers × 14 messages with composition disabled).
- Tick bodies, plans and the 30 canonical decisions are unchanged.
- Deterministic: no LLM, embeddings, clock, randomness or I/O.
- Every number in an answer appears in the opener or the stored contexts.

## 8. Tests

`tests/test_reply_facts.py`: the request table, price, availability (with parts
of day), date, appointment (present and missing), why, count, source, fee,
multi-fact, ambiguous and no-fact questions, customer and merchant voice,
continuity (question → yes), latest-Vera-message priority, Phase 2F isolation,
determinism, template consistency and the CONFIRM/STOP keywords.

## 9. Limitations

- English phrase patterns only (plus `kitna`); paraphrases outside the table
  fall back to the general answer.
- Only facts already sent in the opener, plus the listed customer fields, can
  answer. Plans and evidence are not stored after the tick.
- At most two requests per question.
- "Cancel" is still read as unclear by Phase 2F (see §6).
