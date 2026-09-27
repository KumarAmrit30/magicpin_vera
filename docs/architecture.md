# Vera — Architecture

Phase 1A is the foundation for a deterministic message engine. It implements
the HTTP contract, typed models and in-memory state. Phase 2A adds the decision
domain (`app/engine/`), which is not yet wired into the API. The service still
does **not** decide what to send: there is no planner, no composer and no LLM.

Source of truth for the contract: `magicpin-ai-challenge/challenge-testing-brief.md` §2,
`magicpin-ai-challenge/examples/api-call-examples.md`, and the seed dataset.

## Layers

```text
HTTP (FastAPI routers, app/api/)
  │   request/response models validated by Pydantic (app/models/schemas.py)
  │   context payloads validated per scope             (app/models/domain.py)
  ▼
State (app/state/)
  ├── ContextStore        versioned (scope, context_id) -> latest record
  ├── ConversationStore   conversation_id -> state + ordered turns
  └── SuppressionStore    key -> record with optional expiry
      all owned by one StateContainer attached to app.state
```

- `app/main.py` — `create_app(settings, state)` builds an app that owns its
  settings and a fresh `StateContainer`. There is no module-level mutable
  state besides the ASGI `app` instance, so every test gets isolated stores.
- `app/clock.py` — injectable clock. Stores take a `Clock`; tests use a fake
  clock, so timestamps and uptime are deterministic under test.
- `app/config.py` — `Settings` (team identity via `VERA_*` env vars) and
  logging setup.

## Context versioning

`ContextStore.put()` runs under a lock and returns a `PutResult` whose
`outcome` is one of:

| Outcome     | Condition                    | Store effect      | HTTP |
|-------------|------------------------------|-------------------|------|
| `created`   | nothing stored for the key   | stored            | 200  |
| `replaced`  | incoming version > stored    | atomic replace    | 200  |
| `duplicate` | incoming version == stored   | none (idempotent) | 200  |
| `stale`     | incoming version < stored    | none              | 409  |

A duplicate returns the same `ack_id` and `stored_at` as the original push, so
re-posting is byte-for-byte idempotent. Payloads are deep-copied on write.

Before storing, the payload is validated against the typed model for its scope
(`CategoryContext`, `MerchantContext`, `CustomerContext`, `TriggerContext`).
The models require only join identifiers (`slug`, `merchant_id` +
`category_slug`, `customer_id` + `merchant_id`, `id` + `scope` + `kind`) and
allow unknown keys. The raw payload is what gets stored, so no data is lost.

## Conversations

States: `new`, `qualifying`, `committed`, `waiting`, `completed`, `ended`
(`completed`/`ended` are terminal). Turns are appended in arrival order and
carry `role`, `body`, `sent_at` (from the judge), `recorded_at` (server clock)
and `turn_number`.

Phase 1A reply handling:

1. Unknown `conversation_id` → create it. `judge_simulator.py` replies on
   conversation ids it never received from `/v1/tick`.
2. Backfill `merchant_id`/`customer_id` if the conversation lacked them; log a
   warning on mismatch and keep the stored value.
3. Append the message as a turn.
4. Terminal state → respond `end`. `new` → `waiting`, respond `wait`.
   Any other state is preserved, respond `wait`.

## Suppression

`suppress(key, reason=None, expires_at=None)`, `is_suppressed(key, now=None)`,
`get`, `clear(key)`, `clear_all()`. Expiry is lazy and evaluated against an
optional `now`, so the judge's simulated time can drive it in later phases.
Nothing writes suppression keys yet.

## HTTP contract as implemented

| Endpoint            | Success                                                                                   | Errors |
|---------------------|-------------------------------------------------------------------------------------------|--------|
| `GET /v1/healthz`   | `{status, uptime_seconds, contexts_loaded{category,merchant,customer,trigger}}`           | —      |
| `GET /v1/metadata`  | `{team_name, team_members, model, approach, contact_email, version, submitted_at, name, engine, description}` | — |
| `POST /v1/context`  | 200 `{accepted: true, ack_id, stored_at, outcome}`                                        | 409 `{accepted: false, reason: "stale_version", current_version}`; 400 `{accepted: false, reason, details}` |
| `POST /v1/tick`     | 200 `{actions: []}`                                                                       | 422 (FastAPI validation) |
| `POST /v1/reply`    | 200 `{action: "wait", wait_seconds: 1800, rationale}` or `{action: "end", rationale}`     | 422 (FastAPI validation) |

400 `reason` values: `invalid_scope`, `invalid_context_id`, `invalid_version`,
`invalid_delivered_at`, `invalid_payload`, `invalid_request` (e.g. unparseable JSON).

## Deviations from the official contract (deliberate)

- **Same-version re-post returns 200, not 409.** `challenge-testing-brief.md`
  §2.1 says a re-post "is a no-op" (idempotent), while
  `api-call-examples.md` Example 1.5 shows a 409 `stale_version`. We follow
  the testing brief and the Phase 1A spec: 200, `accepted: true`,
  `outcome: "duplicate"`, same `ack_id`/`stored_at`. It's a one-line change in
  `app/api/context.py` if the judge turns out to expect 409.
- **Extra response fields.** `outcome` on `/v1/context` 200 responses, and
  `name`/`engine`/`description` on `/v1/metadata`. Clients that ignore
  unknown fields are unaffected.
- **Validation errors outside `/v1/context` return FastAPI's 422.** The contract
  only defines an error shape for `/v1/context`. FastAPI still auto-lists a 422
  for `/v1/context` in the OpenAPI document; at runtime that route returns 400.
- **Timestamps must be timezone-aware.** `delivered_at`, `now`, `received_at`
  and trigger `expires_at` without an offset are rejected. Every official
  example uses `Z` or `+05:30`.
- **`POST /v1/teardown`** (optional in testing brief §11) is not implemented.

## Decision Domain — Phase 2A

> **Triggers are evidence, not instructions.**

A trigger such as `ipl_match_today` does not mean "run an IPL promotion". The
official Case Study 5 scores 50/50 precisely because the bot read the IPL
trigger together with the day of week and the merchant's active offer, and
recommended *against* the match-night promo. The decision engine must interpret
every trigger through category, merchant, performance, offers, conversation,
customer and timing, and it may conclude that nothing should be sent.

### Why decisions are separated from messages

```text
context ─► trigger archetype ─► candidates ─► eligibility ─► evidence ─► scoring ─► ranking ─► DecisionPlan ─► composer (Phase 3)
```

A `DecisionPlan` records *what* to do and *why* (action, target, evidence,
CTA intent, send-as identity, suppression key, priority) without any wording.
That keeps decisions testable on their own: they can be asserted against the
30 canonical test pairs without judging prose, and the composer can change
without touching decision logic. Phase 2A implements only the models and pure
primitives in this pipeline, in `app/engine/`. None of it imports FastAPI or
reads application state, the clock, or the network.

| Module | Contents |
|---|---|
| `archetypes.py` | `TriggerArchetype` |
| `actions.py` | `ActionType`, `DecisionScope`, `CTAType`, `SendAs` (re-exported), `CUSTOMER_ACTIONS`, `SEND_AS_BY_SCOPE` |
| `evidence.py` | `Evidence`, `EvidenceSource`, `resolve_field`, `is_grounded` |
| `scoring.py` | weight constants, `validate_score_dimension`, `score_candidate`, `candidate_sort_key`, `rank_candidates` |
| `plans.py` | `DecisionCore`, `DecisionCandidate`, `DecisionPlan`, `make_plan_id` |

### TriggerArchetype

Behavioral categories that say how a trigger should be reasoned about:
`SAFETY_COMPLIANCE`, `ACTIVE_INTENT`, `CUSTOMER_TIMING`, `PERFORMANCE`,
`MARKET_OPPORTUNITY`, `COMPETITIVE`, `OPERATIONS`. Many `trigger.kind` values
(24 distinct kinds in `triggers_seed.json`) will map onto these. **The
`kind → archetype` mapping is not implemented yet.**

### ActionType, DecisionScope, CTAType, SendAs

- `ActionType` is a deliberately small controlled vocabulary: `NO_ACTION`;
  `SEND_INSIGHT`, `SEND_ALERT`; `DRAFT_CAMPAIGN`, `DRAFT_LISTING`, `DRAFT_POST`,
  `DRAFT_MESSAGE`, `DRAFT_ARTIFACT`; `SEND_CUSTOMER_REMINDER`,
  `SEND_CUSTOMER_WINBACK`, `SEND_CUSTOMER_FOLLOWUP`; `RECOMMEND_RETENTION`,
  `RECOMMEND_OPERATIONAL_FIX`; `ASK_MERCHANT`. New actions are added deliberately.
- `DecisionScope` (`MERCHANT`, `CUSTOMER`) is who the action targets, which is
  separate from which context layer the data came from.
- `CTAType` (`NONE`, `YES_NO`, `OPEN_ENDED`, `CONFIRMATION`) is the planned
  CTA intent. It is distinct from the wire-level `app.models.enums.CtaType`
  (`binary_yes_no`, `multi_choice_slot`, ...); Phase 3 maps one to the other.
- `SendAs` is reused from `app.models.enums` rather than redefined, because
  the challenge has one send-as vocabulary: `vera` for merchant-facing messages,
  `merchant_on_behalf` for customer-facing ones.

Target-consistency rules, enforced on candidates and plans:

- `scope=customer` requires `customer_id`.
- `SEND_CUSTOMER_*` actions require `scope=customer`.
- `scope=customer` allows only `SEND_CUSTOMER_*` or `NO_ACTION`.
- A merchant-scoped decision may still reference a `customer_id`, e.g. asking
  the merchant about one of their customers.

### Evidence

`Evidence(source, field, value, formatted, importance)`, frozen:

- `source` is an `EvidenceSource`: `category`, `merchant`, `customer`,
  `trigger` or `conversation`. Nothing else is allowed.
- `field` is a dotted path into that source (`performance.delta_7d.calls_pct`,
  `offers.0.title`).
- `value` must be JSON-compatible.
- `formatted` is the human-readable rendering Phase 3 may quote.
- `importance` is in [0.0, 1.0] and is rejected, not clamped, outside that range.

`is_grounded(evidence, source_data)` checks that the value at `field` really
exists and equals `value`, with the same type, so `18.0` does not match `18`.
This is how fabricated evidence is caught. Selecting evidence from context is
**not implemented yet**.

### DecisionCandidate

A possible action before ranking: the shared decision fields (`trigger_id`,
`archetype`, `scope`, `merchant_id`, `customer_id`, `objective`, `action`,
`evidence`, `selected_offer_id`, `suppression_key`, `expires_at`) plus seven
normalized features: `urgency`, `time_pressure`, `merchant_relevance`,
`conversation_relevance`, `actionability`, `evidence_strength`,
`engagement_potential`. Each feature must be a finite number in [0.0, 1.0];
bools, strings and out-of-range values are rejected. The features are scoring
inputs, not the score. **Nothing computes these features yet.**

### DecisionPlan

The decided action handed to the composer: the shared decision fields plus
`plan_id`, `language_style`, `tone_profile`, `cta_type`, `send_as`,
`priority_score` (0–100), `confidence` (0–1) and `rationale_facts` (ordered
factual strings, never prose). Invariants:

- `send_as` must equal `SEND_AS_BY_SCOPE[scope]`.
- `NO_ACTION` requires `cta_type=NONE`.
- `plan_id` is derived when omitted and verified when supplied.

`confidence` is deterministic decision certainty, not a probability. Phase 2A
only validates and stores it; no confidence algorithm exists yet.

### Scoring

```text
score = urgency                * 25
      + time_pressure          * 15
      + merchant_relevance     * 20
      + conversation_relevance * 15
      + actionability          * 10
      + evidence_strength      * 10
      + engagement_potential   *  5        (max 100)
```

`score_candidate` is pure. It re-validates each feature, which catches
candidates built with `model_construct`, and then sums in decimal arithmetic on
each feature's shortest decimal representation. As a result, mathematically
equal scores are equal floats (`0.1` on every feature scores exactly `10.0`),
so float noise never decides a ranking.

### Deterministic ranking

`candidate_sort_key` gives an ascending key, and `rank_candidates` sorts best
candidate first:

1. higher priority score
2. higher urgency
3. higher evidence strength
4. higher conversation relevance
5. earlier `expires_at`; no expiry sorts after any expiry; timezone-aware
   comparison with no reference to the current time
6. lexical `trigger_id`
7. then `action`, `merchant_id`, `customer_id`, `objective`,
   `suppression_key`, `selected_offer_id`, so the order is total and never
   depends on input order (several candidates can share a trigger)

### Plan IDs

`make_plan_id` computes `"plan_" + sha256(json([merchant_id, customer_id,
trigger_id, objective, action, suppression_key]))[:20]`. The same decision gets
the same ID in any process. Presentation and scoring fields such as
`priority_score`, `confidence`, `cta_type` and `rationale_facts` are excluded,
so re-scoring a decision does not change its identity. Hashing a JSON array
prevents separator collisions.

### NO_ACTION

`ActionType.NO_ACTION` is a valid, first-class decision, not an error. Restraint
is rewarded by the judge ("Restraint is rewarded; spam is penalized"). A
no-action plan still carries its evidence, rationale facts and suppression key,
and it carries no CTA.

### Phase 2A non-goals (not implemented)

- `trigger.kind → TriggerArchetype` mapping
- candidate generation
- feature computation (merchant relevance, time pressure, ...)
- customer eligibility and consent checks
- offer matching
- evidence extraction and selection
- suppression decisions
- conversation intent classification
- confidence algorithm
- `/v1/tick` and `/v1/reply` integration (both still behave exactly as in Phase 1A)
- message composition, templates and LLM calls
