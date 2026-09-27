# Phase 2E — `/v1/tick` Planner

## 1. Purpose

`app/engine/planner.py` connects the frozen decision engine (Phases 2A–2D) to
`POST /v1/tick`. For each available trigger it gets one `DecisionPlan` from the
engine. It then decides which plans become **emitted actions** this tick, and
records only those in the stores.

A `DecisionPlan` is an internal decision. An emitted action is something Vera
actually sends. Only an emitted action opens a conversation or writes a
suppression key.

## 2. Planner architecture

| Layer | Module | Owns |
|---|---|---|
| API | `app/api/tick.py` | request validation, calling `plan_tick`, the `TickResponse` |
| Tick orchestration (2E) | `app/engine/planner.py` | loading contexts, per-trigger engine calls, per-suppression-key de-duplication, the 20-action cap, conversation ids, action construction, commits |
| Selection (2D) | `app/engine/selection.py` | ranking eligible candidates, the winner, confidence |
| Eligibility (2C) | `app/engine/eligibility.py` | every send-time rule (expiry, suppression, evidence, offers, merchant state, …) |
| Generation (2B) | `app/engine/candidates/` | candidates and features |

The planner never scores, generates, judges eligibility or re-ranks. The one
ordering it applies across triggers is Phase 2A's `candidate_sort_key`, applied
to each plan's winning candidate. That is the same total order Phase 2D uses
within a trigger.

Public entry points:

- `plan_tick(state, *, now, available_triggers) -> TickResult(actions, decisions)`
- `load_context(context_store, trigger_id, now)`: builds the `CandidateGenerationContext`, or returns the reason it cannot be built
- `new_conversation_id(plan, now, is_taken)`
- `tick_action(plan, conversation_id) -> TickAction`

`TickResult.decisions` holds one `TriggerDecision` per available trigger:
outcome, candidate and eligible counts, rejection codes, plan and conversation
id. It exists for tests, logs and reports; it is not an API shape.

## 3. `/v1/tick` execution flow

```text
POST /v1/tick {now, available_triggers}
  │  (api/tick.py: validate, delegate)
  ▼
plan_tick                                   under StateContainer.tick_lock
  │
  ├─ for each trigger id in sorted(set(available_triggers)):
  │     load_context          trigger → merchant (trigger.merchant_id) → category (merchant.category_slug)
  │                           → customer (trigger.customer_id, if any); latest stored version of each
  │     generate_candidates   Phase 2B
  │     evaluate_candidates   Phase 2C, now = tick now, suppression read-only (peek)
  │     select_decision       Phase 2D → DecisionPlan
  │
  ├─ drop NO_ACTION plans
  ├─ order plans by candidate_sort_key(winning candidate)
  ├─ walk in order: skip a repeated suppression key, stop emitting at 20
  ├─ allocate conversation ids, build TickActions        (reads stores, writes nothing)
  ├─ create conversations (+ the Vera turn)              first write
  └─ commit suppression keys                             last write
  ▼
TickResponse {actions}
```

Only `available_triggers` are considered. The brief calls the list a hint and
lets the bot use "any subset (or none)"; the planner uses all of them. Triggers
that are stored but not listed are not evaluated.

Per-trigger outcomes:

| Outcome | Meaning | Writes |
|---|---|---|
| `emitted` | the plan became an action | conversation + suppression |
| `no_action` | Phase 2D chose `NO_ACTION` (including the fallback when nothing is eligible, e.g. expired or suppressed) | none |
| `duplicate_suppression_key` | a better-ranked plan already uses the same suppression key this tick | none |
| `over_cap` | 20 actions were already selected | none |
| `unknown_trigger` | no stored trigger with that id | none |
| `missing_context` | the trigger's merchant, merchant's category, or trigger's customer is not stored | none |
| `invalid_context` | the stored contexts are inconsistent (e.g. customer of another merchant), as `CandidateGenerationContext` validates | none |
| `unplannable` | the trigger kind is unmapped, so Phase 2D raises `UnplannableTriggerError` | none |

Missing facts are never filled in. A trigger whose context is incomplete is
skipped for this tick and re-evaluated on the next one.

## 4. Conversation allocation

**A tick never continues an existing conversation.** Testing brief §2.2: "If
you want to start a new conversation, generate any unique string. Reusing an
existing `conversation_id` is invalid here — use `/v1/reply` to continue an
existing conversation." Every emitted action therefore opens a new
conversation. It is created with the action's own `merchant_id`,
`customer_id` and `trigger_id`, in state `new`, with one Vera turn holding the
emitted body (`sent_at` = tick `now`).

No stored conversation is passed to Phase 2C (the tick context has
`conversation=None`), consistent with the Phase 2C design note that "a tick
that opens a new conversation passes no conversation". Consequently:

- A conversation can never be reused for the wrong merchant or customer; none is reused at all.
- The terminal-thread rule (2C rule 12) and the thread part of the nudge limit (rule 13) do not apply to tick sends. Merchant-history nudges still count, because they come from the merchant context. Stopping future ticks after an opt-out relies on suppression keys, e.g. the merchant-wide key `suppress:merchant:<id>`, which reply handling (Phase 2F) is expected to write.

**Ids.** `conv_` followed by 20 hex characters of
`sha256(json([plan_id, now in UTC]))`.

- The same plan at the same tick time always gets the same id.
- A later tick gives a different id, so a re-emission after suppression is lifted opens a distinct conversation.
- If the id is already in the conversation store, or already allocated in this tick, `_2`, `_3`, … is appended until it is free.
- No randomness, no process state. `merchant_id`, `trigger_id` and `plan_id` are never used as the id itself.

## 5. One action per (merchant, conversation); per-key de-duplication

- **The contract rule.** challenge-testing-brief.md FAQ: "Can I send multiple messages in one tick to the same merchant? Yes, but only one `action` per `(merchant_id, conversation_id)` pair per tick. Use a follow-up tick to send more." A merchant may therefore receive several actions in one tick, merchant-facing and customer-facing alike, provided each is on a different conversation. Every action opens a new conversation whose id is unique within the tick (§4), so each `(merchant_id, conversation_id)` pair carries exactly one action by construction, and no plan is ever dropped by this rule. The package defines no per-merchant or per-customer limit, and none is added.
- **One action per suppression key per tick.** Phase 2C reads suppression from the store, which is written only at commit. Without this rule, two triggers sharing a key could both go out in one tick. In the expanded dataset all keys are distinct, so it never fires there.

Plans are walked best-first by `candidate_sort_key` of their winning candidate
(score, urgency, evidence strength, conversation relevance, expiry, trigger id,
identity). The first plan for a key wins. That order is total, so the result
does not depend on input order.

A dropped plan is **not** replaced by its trigger's runner-up candidate, since
that would re-select against Phase 2D. It is simply not emitted and not
committed, so the next tick decides that trigger again.

## 6. 20-action cap

`MAX_ACTIONS_PER_TICK = 20` (testing brief §5). The cap applies after
de-duplication, in the same best-first order. The 21st and later plans get
`over_cap` and nothing is written for them. `TickResponse` also enforces
`max_length=20`. Any number of triggers may be evaluated internally.

## 7. Suppression commit semantics

| Case | Suppression write |
|---|---|
| `NO_ACTION` plan (chosen or fallback) | none |
| ineligible candidate | none (eligibility only `peek`s) |
| eligible, dropped by the cap | none |
| eligible, dropped by suppression-key de-duplication | none |
| emitted | `suppress(plan.suppression_key, reason="emitted trigger=… conversation=…")` |

No TTL (`expires_at=None`). The challenge specifies none, and Phase 2C
requires that none be invented. The key is the trigger's own dedup key (or
`<kind>:<trigger_id>`), so every candidate of that trigger is suppressed
afterwards. A repeated tick on the same state therefore never re-sends.

## 8. State mutation ordering

```text
read contexts + suppression (peek) + conversation ids
  → generate → evaluate → select        (pure)
  → de-duplicate → cap                  (pure)
  → allocate ids → build TickActions    (reads only)
  → create conversations                (write 1)
  → commit suppression                  (write 2)
  → return
```

Nothing is written until every action for the tick has been built. The whole
sequence runs under `StateContainer.tick_lock`, so two concurrent ticks cannot
both read a key as unsuppressed and emit the same plan.

## 9. Determinism guarantees

- Triggers are processed in sorted, de-duplicated order. Cross-trigger ordering uses the total `candidate_sort_key`.
- Conversation ids are hashes of the plan id and tick time, with a store-dependent suffix only on collision.
- There is no wall clock in decisions (the tick's `now` is used throughout), no randomness, and no reliance on set or dict iteration order or object identity.
- Tests cover:
  - identical state and time
  - reordered and repeated `available_triggers`
  - reversed context push order and reversed payload key order
  - a separate process
  - a repeated tick on the same state (nothing re-sent)

## 10. Failure behaviour

- The stores are in-memory and have no transactions. The planner claims no atomicity beyond the tick lock.
- Exceptions are not swallowed. The only per-trigger errors handled are the documented contract outcomes: missing or invalid context, and `UnplannableTriggerError`. Anything else propagates and `/v1/tick` returns 500.
- Because all writes happen after planning, a failure during generation, eligibility or selection writes nothing.
- Conversations are persisted before any suppression key. If conversation creation fails partway, no suppression has been committed, so every plan in that tick is re-evaluated on the next tick. Conversations already created stay behind as orphans and are never reused, since ids are fresh and collision-checked.
- If a suppression write itself failed partway (the in-memory store only raises on an empty key, which the models forbid), the keys already written stay committed for actions the judge never received. Under the current stores this cannot happen, and it is documented rather than engineered around.

## 11. Explicit non-goals

- No changes to Phase 2A–2D semantics (scoring, generation, eligibility, ranking, confidence, plan ids).
- No message composition. `body`, `template_name` and `template_params` are deterministic placeholders built from plan fields, which Phase 3 replaces:
  - `template_name = vera_<action>_v0`
  - `template_params = rationale_facts`
  - `body = "[uncomposed] <objective> | <facts…>"`

  Phase 3 has since replaced these three fields with the composer's output (`vera_<action>_v1`; see [`phase-3-composer.md`](phase-3-composer.md)). The rest of this document is unchanged.
- The planning-to-wire CTA mapping is one-to-one: `yes_no → binary_yes_no`, `open_ended → open_ended`, `confirmation → binary_confirm_cancel`, `none → none`. Phase 3 may refine it, e.g. `multi_choice_slot`.
- No LLM, no embeddings, no randomness.
- No `/v1/reply` decision engine (Phase 2F): no reply classification, intent transitions, auto-reply or hostility handling, conversation state machine, or opt-out suppression writes.
- No per-merchant, per-customer or frequency caps, within a tick or across ticks. The package defines none. A plan dropped by the cap or by suppression-key de-duplication is re-decided on a later tick.
