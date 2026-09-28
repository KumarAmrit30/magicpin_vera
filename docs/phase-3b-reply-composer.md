# Phase 3B — Reply Composer

## 1. Purpose

Phase 2F decides *what* Vera does with an inbound reply (`send`, `wait` or
`end`, the CTA, the state transition and any suppression). Phase 3B decides
*how* a `send` is worded. It replaces the `[uncomposed reply]` placeholder and
changes nothing else.

Code: `app/engine/reply_composer.py`. Integration is one line in
`handle_reply` (`app/engine/reply.py`):

```python
decision = realize(decide_reply(conversation, reading, request), conversation, state.context_store)
```

`realize` runs before `_apply`, so the Vera turn stored on the conversation is
the composed body the judge receives.

## 2. Contract

| Phase 2F response | Phase 3B output                                                            |
|-------------------|----------------------------------------------------------------------------|
| `send`            | same `action`, `cta` and `rationale`; `body` replaced with composed wording |
| `wait`            | unchanged; the wire schema has no body, and `wait_seconds` is untouched     |
| `end`             | unchanged; no body                                                          |

The testing brief (§2.3) defines no body for `wait` or `end`, so only `send`
is ever composed. Classification, `decide_reply`, `SEND_PLANS`, constants and
`_apply` are unchanged.

## 3. Inputs (`reply_context`)

Replies have no `DecisionPlan` or candidate set, so the composer reads only
what the conversation already holds:

- **voice:** from the inbound turn's role. A customer reply is answered as the
  business (`we`), and a merchant reply as Vera (`I`).
- **topic:** a fixed phrase per canonical trigger kind of the conversation's
  trigger (`MERCHANT_TOPICS` / `CUSTOMER_TOPICS`, e.g. `the research update`,
  `your upcoming visit`). It is omitted when there is no trigger or the kind is
  unknown.
- **reason:** the first sentence of the tick's opening message, with the
  salutation or greeting stripped. It is omitted if the opener is missing, is
  still a placeholder, or its first sentence is itself an offer or question.
- **offer:** what Vera last offered to do, extracted from the newest Vera turn
  (`I can …`, `Want me to …?`, `Reply CONFIRM to …`, …). Extractions containing
  `?`, brackets or braces are rejected.
- **greeting:** only when Vera has not spoken in the conversation yet (for
  judge-opened conversations). It uses the Phase 3 `merchant_salutation` /
  `customer_greeting`.
- **earlier bodies:** the Vera turns, used to avoid repeating a body.

Missing inputs are dropped from the sentence plan, never guessed. With no
trigger and no prior Vera turn, a reply is fixed text plus an optional greeting.

## 4. Composition paths

| Intent (2F)       | CTA (2F)                | Shape                                                                        |
|-------------------|-------------------------|------------------------------------------------------------------------------|
| affirmative       | `binary_confirm_cancel` | lead · `Reply CONFIRM and I'll/we'll <offer>, or STOP to end here.`          |
| question (answerable) | `open_ended`        | lead · the requested facts (Phase 3C) · `I/We can <offer>.` · one question   |
| question (general) | `open_ended`           | lead · `What I/we have on record: <next unshared fact or reason>` · `I/We can <offer>.` · one question |
| question (why)    | `open_ended`            | `Here's why I raised it:` · `<reason>` · offer · one question                |
| objection         | `open_ended`            | acknowledgement · `I raised it because of this: <reason>` · `No pressure either way.` · offer · one question |
| off_topic         | `open_ended`            | polite decline · `Coming back to <topic>: I/we can <offer>.` · one question  |
| unclear           | `binary_yes_no`         | `Just to check — want me to <offer>?`                                        |
| auto_reply (1st)  | `binary_yes_no`         | `Looks like an automated reply. When the owner sees this, want me to <offer>?` |

When the offer or topic is missing, the CTA falls back to the topic
(`… take the next step on <topic>`, `Should I continue with <topic>?`) and then
to a generic form (`Should I continue?`).

The affirmative wording asks for an explicit CONFIRM and never claims that
anything was done. The off-topic wording declines without claiming the
capability (it never says "I can" about the off-topic request) and returns to
the trigger.

## 5. CTA rules

- `binary_yes_no` / `open_ended`: exactly one `?`, as the last sentence.
- `binary_confirm_cancel`: `Reply CONFIRM … or STOP to end here.` with no
  question. Both keywords are honoured by Phase 2F (CONFIRM → affirmative,
  STOP → opt-out); see Phase 3C §6 for why CANCEL is no longer advertised.
- `none`: no question and no CONFIRM.

The CTA on the wire is always the Phase 2F CTA.

## 6. Templates

`template_name` is `vera_reply_<key>_v1`, where `<key>` is the intent, `why`
for a why-question that has a recorded reason, or `answer` for a question with
a recognised request (Phase 3C). `template_params` hold only the
inserted values (reason, offer, topic, greeting names). `template` has `{{n}}`
placeholders, and substituting the params gives the body back. Only the body
goes on the wire.

## 7. Determinism and repetition

No clock, randomness, LLM or I/O is used. Each intent has three fixed lead
variants per voice; the first variant whose body is not already a Vera turn in
the conversation is used. If all three have been used, the last sentence gets a
`(Follow-up n)` prefix. The same conversation and inbound text always give the
same body, whatever the `received_at` time.

## 8. Tests

`tests/test_reply_composer.py` covers:

- classification isolation: every representative message on eight seed
  triggers, and a multi-turn script, give the same Phase 2F outcome with
  composition disabled;
- the wire contract across all 25 seed triggers;
- the case-study replies;
- grounding, voice, CTA shapes, template consistency, determinism and
  uniqueness.

## 9. Limitations

- English only.
- Question relevance is handled by Phase 3C
  ([`phase-3c-fact-relevance.md`](phase-3c-fact-relevance.md)).
- Topic phrases are fixed per trigger kind.
- Interpretation of messages (e.g. "What time will you call me tomorrow?" as a
  deferral) belongs to Phase 2F and is not revisited here.
