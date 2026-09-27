# Vera — Architecture (Phase 1A)

Phase 1A is the foundation for a deterministic message engine. It implements
the HTTP contract, typed models and in-memory state. It does **not** decide what
to send: there is no decision engine, no composer and no LLM.

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
