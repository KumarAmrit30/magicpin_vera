# Vera — Architecture

Phase 1A is the foundation for a deterministic message engine. It implements
the HTTP contract, typed models and in-memory state. Phase 2A adds the decision
domain (`app/engine/`); Phase 2B adds trigger classification and grounded
candidate generation (`app/engine/candidates/`). Phase 2C adds eligibility,
Phase 2D winner selection, and Phase 2E wires them into `/v1/tick` through the
tick planner. Phase 2F adds the rule-based `/v1/reply` engine. There is still
no composer and no LLM.

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

Reply handling (Phase 2F, `app/engine/reply.py`; replaces the Phase 1A
placeholder):

1. Unknown `conversation_id` → create it. `judge_simulator.py` replies on
   conversation ids it never received from `/v1/tick`.
2. A conflicting `merchant_id`/`customer_id` → respond `end`, record nothing.
   Ids are backfilled only when the conversation's owner was unknown.
3. Append the message as a turn, read it, and move the state. Hostile merchants
   are also suppressed merchant-wide. Respond `send` / `wait` / `end`; a `send`
   is recorded as a Vera turn. Terminal state → respond `end`.

See [`phase-2f-reply.md`](phase-2f-reply.md).

## Suppression

`suppress(key, reason=None, expires_at=None)`, `is_suppressed(key, now=None)`,
`get`, `clear(key)`, `clear_all()`. Expiry is lazy and evaluated against an
optional `now`, so the judge's simulated time can drive it in later phases.
`peek(key, now)` is the non-mutating read used by candidate eligibility
(Phase 2C); `get` drops expired records. The Phase 2E tick planner writes a
plan's `suppression_key` (no expiry) only for actions it actually emits. The
Phase 2F reply engine writes `suppress:merchant:<id>` (no expiry) when a
merchant replies with hostility.

## HTTP contract as implemented

| Endpoint            | Success                                                                                   | Errors |
|---------------------|-------------------------------------------------------------------------------------------|--------|
| `GET /v1/healthz`   | `{status, uptime_seconds, contexts_loaded{category,merchant,customer,trigger}}`           | —      |
| `GET /v1/metadata`  | `{team_name, team_members, model, approach, contact_email, version, submitted_at, name, engine, description}` | — |
| `POST /v1/context`  | 200 `{accepted: true, ack_id, stored_at, outcome}`                                        | 409 `{accepted: false, reason: "stale_version", current_version}`; 400 `{accepted: false, reason, details}` |
| `POST /v1/tick`     | 200 `{actions: [...]}` (0–20 actions, Phase 2E planner)                                   | 422 (FastAPI validation) |
| `POST /v1/reply`    | 200 `{action: "send", body, cta, rationale}`, `{action: "wait", wait_seconds, rationale}` or `{action: "end", rationale}` (Phase 2F) | 422 (FastAPI validation) |

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
map onto these; the mapping is part of Phase 2B (see below).

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
- `send_as` must equal `SEND_AS_BY_SCOPE[scope]`.
- `NO_ACTION` requires `cta_type=NONE`.

`cta_type` and `send_as` live on `DecisionCore`, so a candidate already carries
its planned CTA and sender. (Phase 2A had them on `DecisionPlan` only; Phase 2B
moved them because generators plan the CTA.)

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
This is how fabricated evidence is caught. Evidence is selected from context by
the Phase 2B generators.

### DecisionCandidate

A possible action before ranking: the shared decision fields (`trigger_id`,
`archetype`, `scope`, `merchant_id`, `customer_id`, `objective`, `action`,
`cta_type`, `send_as`, `evidence`, `selected_offer_id`, `suppression_key`,
`expires_at`) plus seven normalized features: `urgency`, `time_pressure`,
`merchant_relevance`, `conversation_relevance`, `actionability`,
`evidence_strength`, `engagement_potential`. Each feature must be a finite
number in [0.0, 1.0]; bools, strings and out-of-range values are rejected. The
features are scoring inputs, not the score. They are computed by
`app/engine/features.py` (Phase 2B).

### DecisionPlan

The decided action handed to the composer: the shared decision fields plus
`plan_id`, `language_style`, `tone_profile`, `priority_score` (0–100),
`confidence` (0–1) and `rationale_facts` (ordered factual strings, never
prose). `plan_id` is derived when omitted and verified when supplied.

`confidence` is deterministic decision certainty, not a probability. Phase 2A
only validates and stores it; Phase 2D computes it (see below).

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
   `suppression_key`, `selected_offer_id`, `cta_type`, so the order is total and never
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

- `trigger.kind → TriggerArchetype` mapping (done in Phase 2B)
- candidate generation (done in Phase 2B)
- feature computation (done in Phase 2B)
- customer eligibility and consent checks
- offer matching
- evidence extraction and selection (done in Phase 2B)
- suppression decisions
- conversation intent classification
- confidence algorithm
- `/v1/tick` and `/v1/reply` integration (both still behave exactly as in Phase 1A)
- message composition, templates and LLM calls

## Candidate Generation — Phase 2B

Phase 2B turns a trigger plus its context into a list of grounded
`DecisionCandidate`s. It proposes options; it does not choose one. The
pipeline it will feed is *eligibility → scoring/ranking → planning →
composition*.

```text
CandidateGenerationContext ──classify_trigger(trigger)──► TriggerArchetype | None
        │                                                        │
        │                              None ─► [] (unmapped: nothing is guessed)
        ▼                                                        ▼
generate_candidates ──GENERATORS[archetype]──► ArchetypeGenerator.generate
                                                  │  handler for trigger.kind
                                                  ▼
                                               Proposal(s) ──realize──► DecisionCandidate
                                                  (missing required fact ⇒ dropped)
        ◄── grounding gate: every evidence item re-checked with is_grounded ──┘
```

### Modules

| Module | Contents |
|---|---|
| `archetypes.py` | `TRIGGER_KIND_ARCHETYPES`, `TRIGGER_KIND_ALIASES`, `classify_trigger`, `classify_trigger_kind`, `canonical_trigger_kind` |
| `features.py` | the seven feature functions, `compute_features`, conversation-state helpers, `tokens` |
| `candidates/context.py` | `CandidateGenerationContext` (frozen, deep-copied payloads), `ConversationTurnView` |
| `candidates/base.py` | `CandidateGenerator` protocol, `Proposal`, `realize`, `ArchetypeGenerator`, shared lookups |
| `candidates/{safety,intent,customer,performance,opportunity,competitive,operations}.py` | one generator per archetype; one small handler per trigger kind |
| `candidates/registry.py` | `GENERATORS` (read-only archetype → generator map), `generate_candidates` |

### Classification

The mapping is explicit and covers exactly the 26 kinds that occur in the
dataset: 24 in `triggers_seed.json`, plus `appointment_tomorrow` and
`customer_lapsed_soft`, which only `generate_dataset.py` emits. Three
documented aliases share a dataset kind's handling: `research_digest_release`
and `category_research_digest_release` (challenge brief) resolve to
`research_digest`, and `bridal_followup` (Case Study 3) resolves to
`wedding_package_followup`. Matching is exact: there is no case folding or fuzzy
matching. Any other kind is unmapped, `classify_trigger` returns `None`, and no
candidates are produced. Kinds that are only mentioned in prose
(`weather_heatwave`, `local_news_event`, `category_trend_movement`,
`scheduled_recurring`, `unplanned_slot_open`, `festival`) are deliberately
unmapped until real payloads exist.

| Archetype | Kinds |
|---|---|
| SAFETY_COMPLIANCE | `regulation_change`, `supply_alert` |
| ACTIVE_INTENT | `active_planning_intent`, `curious_ask_due`, `dormant_with_vera` |
| CUSTOMER_TIMING | `recall_due`, `appointment_tomorrow`, `trial_followup`, `chronic_refill_due`, `customer_lapsed_soft`, `customer_lapsed_hard`, `wedding_package_followup` |
| PERFORMANCE | `perf_dip`, `perf_spike`, `seasonal_perf_dip`, `milestone_reached` |
| MARKET_OPPORTUNITY | `research_digest`, `festival_upcoming`, `category_seasonal`, `ipl_match_today`, `cde_opportunity` |
| COMPETITIVE | `competitor_opened` |
| OPERATIONS | `renewal_due`, `winback_eligible`, `gbp_unverified`, `review_theme_emerged` |

In the data, `renewal_due` and `winback_eligible` are merchant-scoped and concern
the merchant's own magicpin subscription (`days_remaining`, `renewal_amount`,
`days_since_expiry`). They are account operations, not customer timing.

### Context

`CandidateGenerationContext(category, merchant, trigger, customer?, conversation?, now)`
is frozen, and its payloads are deep-copied on construction, so neither the
caller nor a generator can mutate shared state. Construction validates each
payload against the Phase 1A domain models and rejects inconsistent joins:
merchant and category slug must agree; the trigger and merchant IDs must
agree; a customer is required exactly when the trigger names one, and it must
belong to the merchant. `now` is the simulated decision time and must be
timezone-aware. `conversation` accepts a store `Conversation` or its dict form.
Conversation turns are read from `merchant.conversation_history` (ordered by
timestamp) followed by the live conversation's turns.

### Generators and grounding

A handler returns `Proposal`s. Each proposal names an action, objective and
CTA, plus its `required` and `supporting` evidence. Evidence is only ever built
by resolving a real path in the context (`ctx.evidence(...)`), so a missing
fact comes back as `None` rather than a guess. `realize` drops a proposal
whenever any `required` item is `None`. Every candidate also cites
`trigger.kind` (importance 0.2). Evidence is de-duplicated by path and sorted
by importance, then source, then path. `generate_candidates` re-checks every
evidence item with `is_grounded` and drops any candidate that fails, including
candidates from custom generators. Output within a trigger is sorted by
action, objective, offer, CTA and customer; that order is stable, not a
ranking.

Scope follows the action: `SEND_CUSTOMER_*` (and `NO_ACTION` on a customer
trigger) is customer-scoped and sent as `merchant_on_behalf`; everything else
is merchant-scoped and sent as `vera`, and may reference the customer.
`selected_offer_id` is only ever the ID of an **active** offer in
`merchant.offers`. Nothing is created, discounted or chosen from the category
catalog.

Generated (placeholder) triggers carry `{"placeholder": true}` payloads. For
these, handlers fall back to merchant, customer and category state:
`performance.delta_7d`, `relationship.last_visit`, `review_themes`,
`subscription`, and the category digest. Such fallback evidence is cited with
lower importance, and the placeholder payload itself is never cited. When the
facts that matter cannot be found (an unnamed festival or competitor, a
milestone without numbers), the result is `NO_ACTION`.

### Behavior by archetype

- **Safety/compliance:** `SEND_ALERT`, plus `RECOMMEND_OPERATIONAL_FIX` when the
  digest item lists a required step. A supply recall adds a `DRAFT_MESSAGE` to
  notify chronic-Rx customers and an `ASK_MERCHANT` about stock. If the
  regulation or batches cannot be cited, the only candidate is `NO_ACTION`.
- **Active intent:** an explicit planning message yields `DRAFT_ARTIFACT` with
  CTA `CONFIRMATION`, never a qualifying question. A related active offer adds
  a `DRAFT_CAMPAIGN`. `ASK_MERCHANT` appears only when no merchant message backs
  the intent. `curious_ask_due` asks an open question unless the merchant's
  latest turn is an unanswered request. In that case the request is answered
  first, with the draft type Vera had offered (posts → `DRAFT_POST`,
  list/message → `DRAFT_MESSAGE`, ...). `dormant_with_vera` yields
  `ASK_MERCHANT`, plus a `SEND_INSIGHT` on the merchant's largest 7-day move.
- **Customer timing:** each kind has a `CustomerMoment` (reminder, follow-up or
  win-back) grounded in its payload facts. A slot or option list makes the CTA
  `CONFIRMATION`. Every moment also yields a merchant-facing `DRAFT_MESSAGE`
  for approval; lapse moments add `RECOMMEND_RETENTION` when an aggregate shows
  a lapse pattern. Consent is not evaluated here.
- **Performance:** a dip yields `SEND_ALERT`, plus `RECOMMEND_OPERATIONAL_FIX`
  only for recorded listing gaps, and `DRAFT_CAMPAIGN` only per existing
  active offer. A spike yields `SEND_INSIGHT` (CTA `NONE`), `DRAFT_POST` when
  a post drove it, and offer campaigns; it is never alerted or "fixed". An
  expected seasonal dip yields a reframing `SEND_INSIGHT`, `NO_ACTION`, and
  `RECOMMEND_RETENTION` when a member count exists. If the numbers contradict
  the trigger's direction, the result is `NO_ACTION`.
- **Market opportunity:** for a weekend IPL match, where the category digest
  shows weekend matches underperform, the result is a contrarian
  `SEND_INSIGHT` plus a delivery-first `DRAFT_CAMPAIGN` on the existing offer
  (Case Study 5). A match in another city yields `NO_ACTION`. For festivals,
  categories outside `category_relevance` get `NO_ACTION`; festivals more than
  45 days out also add `NO_ACTION`. A research digest yields `SEND_INSIGHT`,
  citing merchant cohorts that match the item's patient segment.
- **Competitive:** competitor facts come only from the trigger payload. With no
  named competitor, the result is `NO_ACTION`, plus `DRAFT_LISTING` on the
  merchant's top positive review theme, without mentioning a competitor.
- **Operations:** renewal is raised only within 30 days and only for an
  active or trial subscription. A value recap is added only when some 7-day
  metric is up. A merchant winback, a GBP verification or a review theme
  whose premise the merchant's state contradicts yields `NO_ACTION`.

### Feature calculation (`features.py`)

All features are rounded to 4 decimals and lie in [0, 1].

| Feature | Computation |
|---|---|
| urgency | `trigger.urgency / 5`; 0 if absent |
| time_pressure | nearest of `trigger.expires_at` and a fact-backed deadline (due date, run-out date, festival date, match time, renewal date), stepped: ≤24h 1.0, ≤72h 0.8, ≤7d 0.6, ≤14d 0.4, ≤30d 0.2, else 0; passed deadline → 0; `appointment_tomorrow` → 1.0 |
| merchant_relevance | `0.3 + 0.1 × (merchant/customer facts cited)`, `− 0.1` when the candidate's scope differs from `trigger.scope` (see below) |
| conversation_relevance | 1.0 continues an explicit merchant request; 0.9 topical and merchant engaged (0.3 if the action would only restate it); 0.6 topical; 0.3 engaged but off-topic; 0.1 cold; 0 with no conversation |
| actionability | per-action base (ask 0.8, alert 0.7, insight 0.6, recommend/customer send/draft message 0.5, other drafts 0.4) `+ 0.15 × concrete assets` (offer, slots, required step, ...) |
| evidence_strength | `0.7 × max(importance) + 0.3 × min(1, (count − 1) / 4)` |
| engagement_potential | per-action base, ±0.2 for merchant engagement / unresponsiveness (history tags and signals); for customer sends, a shift by customer state |

`NO_ACTION` candidates carry zero urgency, time pressure, conversation
relevance and engagement, and full actionability. They compete on
merchant relevance and evidence strength, i.e. on how strongly the state
supports staying quiet. Topic matching uses normalized word sets (`tokens`), not
substring search.

**Trigger-scope alignment in `merchant_relevance`.** `merchant_relevance`
measures how directly a candidate concerns the situation that raised the
trigger. Each grounded merchant or customer fact it cites adds one step of 0.1.
A trigger's `scope` (`merchant` | `customer`, required by the brief's
`TriggerContext`) names the party whose situation raised it. Customer context
exists only for `scope=customer`, and customer-scoped triggers (recall,
refill, lapse, appointment, trial follow-up) are sent `merchant_on_behalf`.
A candidate addressed to a different party than the declared scope reaches
that situation only at one remove. For example, a merchant-facing
`DRAFT_MESSAGE` for Priya's recall is about Priya's recall only through the
merchant, so it forgoes one fact step: `− 0.1`, the feature's own unit,
equal to 2 points at weight 20.

The rule depends only on the two scopes, never on the action type, and it
applies in both directions. It is a discount on the indirect candidate, not
a bonus for alignment. A bonus would saturate at 1.0 for some candidates and
move them unevenly. With the discount, every directly aligned candidate keeps
exactly its previous value. Today merchant triggers produce only
merchant-scoped candidates, so their features, scores and winners are
unchanged. On customer triggers, the customer send and the draft share
evidence, assets, topic and deadline, so the discount decides between them
only when their engagement estimates are within 2 points. A draft still wins
when the merchant's engagement clearly outweighs the customer's, e.g. an
engaged merchant and a churned customer on a winback.

### Phase 2B non-goals (not implemented)

- ranking candidates into a winner (`best_candidate`)
- customer eligibility, consent, frequency caps, suppression decisions
- offer selection across multiple offers (each active offer is its own candidate)
- `/v1/tick` and `/v1/reply` integration (both still behave exactly as in Phase 1A)
- planning, confidence, message composition, templates and LLM calls

## Eligibility & Suppression — Phase 2C

`app/engine/eligibility.py` sits between generation and ranking:
`evaluate_eligibility(candidate, context, suppression, *, now) -> EligibilityResult(candidate, eligible, reasons)`.
It re-validates Phase 2A invariants and checks trigger, merchant, customer and
conversation integrity. It re-grounds every evidence item against the current
context and checks trigger expiry (`expires_at`), the selected offer, Phase 2B
merchant premises, closed conversations, the §12.5 unanswered-nudge limit, the
candidate's `suppression_key`, and the merchant-wide suppression key.

Each failed rule adds one structured `EligibilityReason(code, detail, source)`.
`NO_ACTION` is exempt from the send-only rules.

Eligibility never ranks, selects or de-duplicates. It only reads suppression
(`SuppressionReader.peek`), and writing keys after a send belongs to the commit
phase. Rules, sources and documented uncertainties are in
[`phase-2c-eligibility.md`](phase-2c-eligibility.md).

## Candidate Scoring & Winner Selection — Phase 2D

`app/engine/selection.py`: `select_decision(context, eligibility_results) -> DecisionPlan`.

```text
EligibilityResult[]  ──filter eligible──►  score_candidate()  ──►  rank_candidates()  ──►  winner  ──►  DecisionPlan
                                          (Phase 2A, unchanged)   (Phase 2A, unchanged)          confidence computed here
```

- **Eligibility first.** The selector takes Phase 2C `EligibilityResult`s, not raw candidates, and ranks only `eligible=True` ones. A rejected candidate never competes, whatever its score.
- **Scoring** is the Phase 2A weighted sum: urgency 25, time pressure 15, merchant relevance 20, conversation relevance 15, actionability 10, evidence strength 10, engagement 5 (total 100). Features are used as generated.
- **Ranking** is the Phase 2A `candidate_sort_key`. The order is: higher score, higher urgency, higher evidence strength, higher conversation relevance, earlier `expires_at` (no expiry last), lexical `trigger_id`, then `action`, `merchant_id`, `customer_id`, `objective`, `suppression_key`, `selected_offer_id` and `cta_type`. It is total, so input order never matters.
- **No preferences.** No scope, action-type or `NO_ACTION` preference is added. `rank_eligible(results)` returns a debugging view of the ranking: `RankedCandidate(rank, decision_id, score, tie_break, candidate)`.
- **`NO_ACTION`** competes like any candidate. Its features (zero urgency, time pressure, conversation relevance and engagement; full actionability) mean it competes on merchant relevance and evidence strength.
- **Fallback when nothing is eligible.** If there are no candidates, or none is eligible, the result is a `NO_ACTION` plan for the context's trigger:
  - objective `no candidates for this trigger` or `no eligible candidates for this trigger`
  - no evidence
  - rationale facts that count candidates and list the rejection codes
  - priority 0, confidence 1.0 (a forced decision)
  - scope customer when the trigger names a customer, as in Phase 2B

  If the trigger kind is unmapped there is no archetype, so no plan can be built and `UnplannableTriggerError` is raised.
- **Plan construction.** The winner's decision fields are copied verbatim: trigger, archetype, scope, merchant, customer, objective, action, CTA, send-as, evidence, selected offer, suppression key and expiry. The plan adds:
  - `priority_score`: the winner's score
  - `confidence`: see below
  - `rationale_facts`: the evidence `formatted` strings, most important first, stable and de-duplicated

  Candidates carry no `language_style` or `tone_profile`, so these stay unset. `plan_id` is the Phase 2A identity hash. Per-offer candidates of one trigger share a `plan_id`, because the identity excludes `selected_offer_id`; only one of them can be selected.
- **Confidence** is decision certainty, computed after the winner is fixed and never used to rank:
  `0.5·score/100 + 0.3·min(1, (score − runner_up)/20) + 0.2·evidence_strength`, with full separation when there is no runner-up, rounded half-even to 4 places. The result is in [0, 1].

### Phase 2D non-goals (not implemented)

- `/v1/tick` and `/v1/reply` integration, and multi-trigger tick assembly (Phase 2E)
- suppression writes on send
- reply decisions, message composition, templates, CTA wire rendering and LLM calls

## Tick Planner — Phase 2E

`app/engine/planner.py`: `plan_tick(state, *, now, available_triggers) -> TickResult`. `/v1/tick` only delegates to it.

```text
available_triggers ─► load contexts ─► generate (2B) ─► evaluate (2C) ─► select (2D) ─► drop NO_ACTION
  ─► order by candidate_sort_key ─► one per suppression key ─► first 20
  ─► TickActions ─► create conversations ─► commit suppression ─► response
```

- Orchestration only: each trigger's plan is exactly the Phase 2D plan. Across triggers, the Phase 2A sort key of each plan's winning candidate orders the plans.
- Every action opens a new conversation with a fresh deterministic id (testing brief §2.2); a tick never reuses a conversation and passes none to Phase 2C.
- Suppression is committed only for emitted actions, after their conversations are created. `NO_ACTION`, ineligible, de-duplicated and over-cap plans write nothing.
- Message fields (`body`, `template_name`, `template_params`) come from the Phase 3 composer; every other field is copied from the plan.

Details, outcome codes, failure behaviour and non-goals: [`phase-2e-planner.md`](phase-2e-planner.md).

## Reply Engine — Phase 2F

`app/engine/reply.py`: `handle_reply(state, request) -> ReplyDecision`. `/v1/reply` only delegates to it and runs under `state.tick_lock`, so replies and ticks do not interleave.

```text
conversation lookup/create ─► ownership check ─► append turn ─► read reply (+ sender auto-reply streak)
  ─► decide (send / wait / end + next state + optional merchant suppression) ─► apply ─► response
```

- Rule-based, with no candidate generation and no tick. Off-topic replies are redirected to the conversation's trigger.
- Ended and completed conversations never resume. Merchant hostility writes `suppress:merchant:<id>` with no expiry, which Phase 2C's `merchant_suppressed` rule applies to later ticks.
- Nudge counting stays in Phase 2C. The engine only records inbound turns and its own sends.

Details and contract sources: [`phase-2f-reply.md`](phase-2f-reply.md).

## Message Composer — Phase 3

`app/engine/composer.py`: `compose(plan, context) -> ComposedMessage`. The planner calls it in `tick_action` for each emitted plan; nothing upstream of the selected plan changes.

```text
DecisionPlan + CandidateGenerationContext
  ─► grounded facts (plan evidence, re-checked with is_grounded; internal labels dropped)
  ─► salutation / lead facts / digest citation / proposal / one CTA
  ─► ComposedMessage(body, cta, send_as, template_name, template_params, template, facts_used)
```

- Realization only: no selection, ranking, suppression or conversation-state change. `NO_ACTION` raises `CompositionError`.
- `template` is the body with `{{n}}` in place of each positional parameter; substituting `template_params` gives the body back.
- Reply bodies are worded separately (Phase 3B, below).

Details: [`phase-3-composer.md`](phase-3-composer.md).

## Reply Composer — Phase 3B

`app/engine/reply_composer.py`: `realize(decision, conversation, contexts)`. `handle_reply` calls it after `decide_reply` and before `_apply`, so the stored Vera turn and the wire body are the same text.

```text
ReplyDecision (Phase 2F, authoritative)
  ─► send? no  ─► unchanged (wait / end have no body)
  ─► send? yes ─► reply_context(conversation, contexts): voice, topic (trigger kind), reason (opener's first fact),
                  offer (latest Vera offer), greeting (only before Vera's first turn), earlier bodies
              ─► compose_reply(intent, cta, context) ─► body swapped in; every other field kept
```

- No `DecisionPlan`: replies have no candidate set, so the composer reads only the conversation and the contexts named by its ids.
- Reuses `TemplateWriter`, `merchant_salutation` and `customer_greeting` from the Phase 3 composer.

Details: [`phase-3b-reply-composer.md`](phase-3b-reply-composer.md).
