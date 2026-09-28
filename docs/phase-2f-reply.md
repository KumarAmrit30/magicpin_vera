# Phase 2F — `/v1/reply` Conversation & Reply Decision Engine

## 1. Purpose

`/v1/reply` continues the conversation the judge names. It reads the inbound
message with fixed rules, moves the conversation's state, writes suppression
when the contract requires it, and answers `send`, `wait` or `end`.

The engine does not generate candidates, run a tick, or compose prose.
`decide_reply` still produces the structured placeholder body described below;
since Phase 3B, `handle_reply` replaces it with composed wording before the turn
is stored or returned ([`phase-3b-reply-composer.md`](phase-3b-reply-composer.md)).

Code: `app/engine/reply.py`. `app/api/reply.py` is a thin wrapper around
`handle_reply`. The only other production change is one read-only method,
`ConversationStore.find`.

Sources, in priority order: `judge_simulator.py`, `challenge-testing-brief.md`
§2.3 and §4 (Phase 4 replay), `examples/api-call-examples.md` 2.4–2.7 and
4.1–4.3, and `challenge-brief.md` §8, §9 (Patterns B and D) and §12.

## 2. `/v1/reply` execution flow

```text
ReplyRequest (Pydantic; 422 on invalid)
  └─ under state.tick_lock (serialized with /v1/tick):
       get_or_create(conversation_id)            unknown id -> new conversation (state new)
       ownership_conflict?  -> end, nothing recorded
       backfill ids (only if the owner was unknown)
       sender_messages: sender's earlier inbound texts (their conversations, arrival order)
       append inbound turn
       read_reply([...earlier, message])         -> ReplyReading(intent, cue, auto_reply_streak)
       decide_reply(conversation, reading, req)  -> ReplyDecision(response, state, suppression)
       apply: set_state -> merchant suppression (no expiry) -> Vera turn (send only)
  └─ response: SendReply | WaitReply | EndReply
```

`classify_text`, `read_reply` and `decide_reply` are pure functions. Only
`handle_reply` touches the stores.

## 3. Conversation state machine

The engine uses the existing `ConversationState` enum and adds no states.

| From \ reply | affirmative | question / objection / off-topic / unclear | deferral | auto-reply 1 / 2 / ≥3 | not interested / opt-out / hostile |
|---|---|---|---|---|---|
| `new` | `committed` | `qualifying` | `waiting` | `new` / `waiting` / `ended` | `ended` |
| `qualifying` | `committed` | `qualifying` | `waiting` | `qualifying` / `waiting` / `ended` | `ended` |
| `committed` | `committed` | `committed` (never back to qualifying) | `waiting` | `committed` / `waiting` / `ended` | `ended` |
| `waiting` | `committed` | `qualifying` | `waiting` | `waiting` / `waiting` / `ended` | `ended` |
| `ended`, `completed` | unchanged, `end` | unchanged, `end` | unchanged, `end` | unchanged, `end` | unchanged, `end` |

- A tick creates conversations as `new`, with one Vera turn.
- An unknown conversation also starts as `new`.
- `completed` is never set by Phase 2F. It stays terminal and is reserved for a
  later phase that confirms an executed action.
- An ended conversation never resumes (api-call-examples 2.6).

## 4. Reply classification

`classify_text` lowercases the message, drops apostrophes and collapses
whitespace. It then applies the ordered rules in `RULES`, and the first match
wins:

1. `hostile`
2. `opt_out`
3. `auto_reply`
4. `not_interested` (phrases)
5. `deferral`
6. `not_interested` (bare "no", except "no problem/worries/rush")
7. `off_topic`
8. `objection`
9. `affirmative` (including a bare option number such as "1")
10. `question`
11. `unclear`

Negatives and deferrals come before affirmatives, so "ok, but later" is a
deferral. Objections and off-topic asks also come before affirmatives, so "ok,
can you file my GST?" is off-topic.

`read_reply` adds the auto-reply rules, which work over the sender's message
sequence:

- A message matching a canned-text pattern is an auto-reply.
- The same message three times in a row, verbatim, is an auto-reply
  (challenge-brief §12.1). All three messages count, so the streak is 3 at
  once. This rule only overrides readings with no explicit meaning (off-topic,
  objection, question, unclear); a repeated "yes" or "STOP" keeps its meaning.
- `auto_reply_streak` is the number of consecutive auto-replies ending at the
  latest message.

## 5. Affirmative behavior

Affirmatives include "yes", "ok", "let's do it", "go ahead", "I want to join",
"Mujhe magicpin judrna hai" and "1".

The engine answers `send` with `cta: binary_confirm_cancel`, and the state
becomes `committed`. This follows api-call-examples 4.2 (action mode) and
challenge-brief §9 Pattern D and §12.2.

The placeholder body is:
`[uncomposed reply] action: confirm and proceed with the next step the <merchant|customer> agreed to | trigger=<id> turn=<n>`.
It contains no qualifying question, which is what the judge simulator's
`intent_transition` check looks for.

## 6. Negative behavior

`not_interested` ("not interested", "no thanks", "don't need", a bare "no")
answers `end` and moves the conversation to `ended`
(challenge-testing-brief §2.3; challenge-brief §12.5). No suppression key is
written. Other triggers for the merchant may still be sent by later ticks.

`deferral` ("later", "busy", "not now", "baad mein") answers `wait` with
`wait_seconds: 1800` ("Merchant asked for time; back off 30 min",
testing brief §2.3). The state becomes `waiting`, and a later genuine reply
resumes the conversation.

## 7. Question/objection behavior

Both signal continued engagement, so the engine answers `send`:

- A question gets `cta: open_ended` with the placeholder
  `answer: reply from the recorded contexts only, then restate the pending next step`.
- An objection gets `cta: open_ended` with the placeholder
  `objection: acknowledge the concern and restate the grounded value of the proposal`.
- `unclear` gets `cta: binary_yes_no` with the placeholder
  `clarify: restate the pending proposal as a single yes/no choice`.

The state becomes `qualifying`; a `committed` conversation stays `committed`.
No answer is invented at this phase.

## 8. Off-topic behavior

Off-topic asks include GST, tax filing, loans, insurance and passports. The
engine answers `send` with `cta: open_ended` and the placeholder
`off_topic: politely decline the unrelated request and redirect to the original trigger`
(api-call-examples 2.7 and 4.3).

The conversation's `trigger_id` is unchanged. No candidate generation, tick,
new conversation or suppression is involved.

## 9. Auto-reply behavior

The engine follows api-call-examples 4.1, where the same canned reply arrives
four times:

| Sender's auto-replies in a row | Response | State |
|---|---|---|
| 1 | `send`, `cta: binary_yes_no` (one prompt for the owner) | unchanged |
| 2 | `wait`, `wait_seconds: 86400` | `waiting` |
| ≥ 3 | `end` | `ended` |

The streak belongs to the sender, meaning the merchant (or the
merchant/customer pair), and runs across all of that sender's conversations in
arrival order. The canned text comes from the merchant's WhatsApp Business
account, not from one thread. `judge_simulator.py` sends it on `conv_auto_1`
through `conv_auto_4`, so the bot exits on the third. Any genuine reply resets
the streak.

An auto-reply is neither interest nor rejection, so the first one leaves the
state unchanged and writes no suppression.

**Ambiguity:** api-call-examples 2.5 answers a single auto-reply with `wait`
14400. Example 4.1, the Phase 4 replay scenario, answers the first one with a
single prompt. Challenge-brief Pattern B ("tried once, then stopped") agrees
with 4.1, so 4.1 is implemented. The simulator treats a first-turn `send` as a
warning, not a failure.

## 10. Hostility behavior

Hostile markers include "useless", "spam", "bothering", "harass…",
"nonsense" and "bakwas" (api-call-examples 4.3; the simulator's hostile
scenario).

- **Merchant hostility:** `end`, state `ended`, plus a merchant-wide
  suppression `suppress:merchant:<merchant_id>` with **no expiry**
  (4.3: "Suppressing all triggers for this merchant").

  The "30 days" in 4.3 is illustrative. It appears only inside that example's
  free-text rationale. The same example's "acceptable alternative" promises
  "I won't message again" with no duration. The testing brief (replay 3) and
  `judge_simulator.py` (which checks only `end` or an apology) state no
  duration, and nothing in the package sets one. So no TTL is invented. This
  matches every other suppression write here: Phase 2E's trigger keys also
  have no expiry.
- **Customer hostility:** `end` and `ended` only. A customer's reply does not
  suppress the merchant.
- **Merchant unknown:** `end`, and nothing is written.

The testing brief's replay 3 sends abuse, then an unrelated question. After
the hostile turn closes the conversation, the question gets `end` (2.6: no
further messages on that `conversation_id`).

## 11. Opt-out behavior

Opt-out messages include "Stop messaging me", "STOP", "unsubscribe" and "don't
message me". They answer `end`, the state becomes `ended`, and **no key** is
written.

api-call-examples 2.6 ("Not interested. Stop messaging me.") says
"suppressing this conversation_id for future ticks". The ended conversation is
that suppression: Phase 2C's `conversation_closed` rule and this engine both
read it, and ticks never reuse a conversation id.

Hostile and non-hostile opt-outs stay separate because the package treats them
differently: 2.6 is conversation-scoped, while 4.3 is merchant-wide.

## 12. Suppression writes

| Reply | Key | Expiry |
|---|---|---|
| merchant hostility | `suppress:merchant:<merchant_id>` (Phase 2C `merchant_suppression_key`) | none (the package specifies no duration) |
| anything else | none | — |

- **Repeat hostility.** An existing indefinite record is kept, including its
  original reason. An expiring record, for example one set by an operator,
  becomes indefinite.
- **Replies to ended conversations** answer `end` and write nothing.
- **Visible to future ticks.** Phase 2C rule `merchant_suppressed` blocks every
  candidate, merchant- or customer-scoped, for that merchant.
  This is tested in `tests/test_reply_dataset.py`.
- **Customer-wide opt-out cannot be enforced.** Phase 2C reads only the
  candidate's own key and the merchant key, so a customer-wide key would not
  be read without changing Phase 2C. Customer opt-outs therefore end only
  their conversation.

## 13. Nudge-count interaction

The engine has no nudge counter. Phase 2C's `_unanswered_streak` remains the
only one: it counts bot turns since the recipient last replied.

Phase 2F keeps that count correct by recording turns faithfully:

- Each inbound reply is appended as a `merchant` or `customer` turn, which
  resets the streak.
- Each `send` appends one Vera turn, which counts as one unanswered bot turn.
- `wait` and `end` append nothing.

At tick time Phase 2C sees only the merchant context's `conversation_history`,
because the tick passes no conversation. So the three-unanswered-nudges block
on ticks is unchanged. When a live conversation is passed to Phase 2C, a
genuine reply resets the streak. Tests: `tests/test_reply_dataset.py`,
nudge section.

Following Phase 2C semantics, any recipient turn resets the streak, including
auto-replies. The auto-reply streak in §9 handles canned replies instead.

## 14. Unknown conversation handling

- **Unknown `conversation_id`:** created with the request's `merchant_id` and
  `customer_id`, `trigger_id: null` and state `new`, then handled normally.
  This keeps the Phase 1A behavior the simulator relies on. No other
  conversation is touched or linked.
- **Unknown owner:** a conversation opened without ids takes the first ids
  supplied later (backfill).
- **Ownership conflict:** a conflicting `merchant_id`, or a `customer_id` on a
  merchant thread or a different customer's thread, answers
  `end` "Reply does not belong to this conversation (…); not recorded". The
  conversation, its turns and the suppression store are left untouched.
- **Missing `customer_id` on a customer thread:** accepted, because the field
  is optional in the contract.

## 15. Determinism

- **No randomness or wall clock.** Decisions depend only on the stored
  conversation, the sender's earlier messages, the request, and
  `received_at`. Time enters only as `received_at` (suppression expiry, turn
  timestamps).
- **Unique bodies.** The placeholder body includes the trigger id and the
  request's `turn_number`, so bodies do not repeat within a conversation
  (anti-repetition, failure mode F.5).
- **Tested.** Repeated sessions produce identical responses and conversation
  dumps. Interleaving replies from different senders does not change any
  conversation's outcome. A full tick → reply → tick session is
  byte-identical across runs.
- **Retries are appended.** Identical retried requests are recorded again, as
  in Phase 1A. The history stays consistent, but a retried canned message
  advances the auto-reply streak.

## 16. Error behavior

- **Invalid payload:** 422 from FastAPI validation, as before. `from_role`
  must be `merchant` or `customer`, `turn_number` ≥ 1, `received_at`
  timezone-aware, and `conversation_id` non-empty.
- **Empty `message`:** allowed by the schema. It reads as `unclear` and
  answers `send` with a clarifying placeholder.
- **Closed conversation:** 200 `end`. The turn is recorded; nothing else
  changes.
- **Unknown conversation:** created (§14).
- **Ownership conflict:** 200 `end`, nothing recorded (§14).
- **No new status codes.**

## 17. Explicit Phase 3 non-goals

Not implemented:

- message wording and personality
- LLM or VLM reply understanding
- CTA copy
- language matching per turn
- answering questions with facts
- executing committed actions (and so the `completed` state)
- customer-wide opt-out enforcement (needs a Phase 2C read rule)
