# Vera — magicpin AI Challenge

Vera is magicpin's WhatsApp assistant for merchant growth. This repository is a
**deterministic** (no-LLM) implementation of the challenge bot: an HTTP service
the magicpin judge harness pushes context to, ticks periodically, and plays the
merchant/customer against.

## Current scope: Phase 1A (foundation)

Implemented:

- The five `/v1` endpoints from `challenge-testing-brief.md` §2, with Pydantic-validated request/response models
- Typed models for the four context payloads (category, merchant, customer, trigger), checked against the official seed dataset
- A versioned context store: first version stored, higher version replaces atomically, same version is an idempotent no-op, lower version is rejected
- A conversation store (states `new`/`qualifying`/`committed`/`waiting`/`completed`/`ended`, ordered turns)
- A suppression-key store with optional expiry

In Phase 1A, `/v1/tick` returned `{"actions": []}`; since Phase 2E it runs the
decision engine (below). `/v1/reply` still records the message and answers
`wait` (or `end` for an already-closed conversation). No messages are composed.

## Phase 2A — Decision Domain

Implemented in `app/engine/` (not yet connected to the API):

- decision domain models (`TriggerArchetype`, `ActionType`, `DecisionScope`, `CTAType`, `Evidence`, `DecisionCandidate`, `DecisionPlan`)
- candidate scoring primitives (weighted 0–100 score, validated 0–1 features)
- deterministic plan IDs
- deterministic ranking primitives

## Phase 2B — Candidate Generation

Implemented in `app/engine/` (not yet connected to the API):

- explicit `trigger.kind → TriggerArchetype` mapping for all 26 dataset kinds (unknown kinds stay unmapped)
- one candidate generator per archetype in `app/engine/candidates/`, dispatched through a registry
- `generate_candidates(context)`: grounded `DecisionCandidate`s with planned CTA, `send_as` and all seven features (`app/engine/features.py`)
- every evidence item is re-checked against the context; ungrounded candidates are dropped

## Phase 2C — Eligibility & Suppression

Implemented in `app/engine/eligibility.py` (not yet connected to the API):

- `evaluate_eligibility(candidate, context, suppression, *, now)` returns `EligibilityResult(candidate, eligible, reasons)` with stable, machine-readable reason codes
- checks: structure, trigger/scope integrity, evidence re-grounding against the current context, `expires_at`, active offer, merchant-state premises, closed conversation, the 3-unanswered-nudge limit, and suppression by `suppression_key` or merchant-wide key
- suppression is read-only during evaluation (`SuppressionStore.peek`); nothing is written and no TTL is invented

## Phase 2D — Scoring & Winner Selection

Implemented in `app/engine/selection.py` (not yet connected to the API):

- `select_decision(context, eligibility_results)` ranks only eligible candidates with the Phase 2A `score_candidate` / `rank_candidates` and returns exactly one `DecisionPlan`
- a `NO_ACTION` plan when there are no candidates or none is eligible; `NO_ACTION` candidates otherwise compete on their features
- deterministic confidence (score, margin over runner-up, evidence strength), computed after selection

## Phase 2E — `/v1/tick` Planner

Implemented in `app/engine/planner.py`; `/v1/tick` delegates to it:

- `plan_tick(state, now=..., available_triggers=...)` runs each available trigger through Phases 2B–2D and turns the resulting plans into at most 20 actions
- `NO_ACTION` is never emitted; at most one action per `(merchant_id, conversation_id)` pair (testing brief FAQ; guaranteed by unique ids) and per suppression key per tick, best-ranked first by the Phase 2A sort key
- each emitted action opens a new conversation with a fresh deterministic id; existing conversations are never reused by a tick (testing brief §2.2)
- suppression keys are committed only for emitted actions, after their conversations are created
- `body`/`template_name`/`template_params` are deterministic placeholders built from the plan until Phase 3 composes messages

Not yet implemented:

- `/v1/reply` decision engine (Phase 2F)
- message composition (Phase 3)

Design principle: *triggers are evidence, not instructions.* See the "Decision Domain — Phase 2A",
"Candidate Generation — Phase 2B", "Eligibility & Suppression — Phase 2C" and "Candidate Scoring & Winner Selection — Phase 2D" sections of
[`docs/architecture.md`](docs/architecture.md), [`docs/phase-2c-eligibility.md`](docs/phase-2c-eligibility.md) and [`docs/phase-2e-planner.md`](docs/phase-2e-planner.md).

## Architecture

```text
app/
├── main.py            create_app(): owns settings + StateContainer, mounts routers under /v1
├── config.py          Settings (VERA_* env vars), logging setup
├── clock.py           injectable UTC clock
├── api/               health.py (healthz, metadata), context.py, tick.py, reply.py, deps.py
├── models/            enums.py, schemas.py (API contract), domain.py (context payloads)
├── engine/            decision domain: archetypes, actions, evidence, features, scoring, plans, eligibility, selection, planner (no FastAPI)
│   └── candidates/    context, registry, and one generator per trigger archetype
└── state/             context_store.py, conversation_store.py, suppression_store.py, container.py
tests/                 store, HTTP contract, domain-model, decision-domain and candidate-generation tests
docs/architecture.md   layer design, versioning table, documented deviations
magicpin-ai-challenge/ official challenge package, vendored unmodified
```

All state is in memory and lives for the life of the process.

## Install

Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload --port 8080
```

Optional environment variables for `/v1/metadata`: `VERA_TEAM_NAME`,
`VERA_TEAM_MEMBERS` (comma-separated), `VERA_CONTACT_EMAIL`,
`VERA_SUBMITTED_AT`. `LOG_LEVEL` defaults to `INFO`.

Docker:

```bash
docker build -t vera .
docker run -p 8080:8080 vera
```

## Test

```bash
pytest -q
```

## Endpoints

| Method | Path           | Current behavior |
|--------|----------------|------------------|
| GET    | `/v1/healthz`  | `{"status": "ok", "uptime_seconds": N, "contexts_loaded": {...}}` |
| GET    | `/v1/metadata` | Bot identity (team fields from env, `engine: "deterministic"`, `model: "none"`) |
| POST   | `/v1/context`  | 200 created/replaced/duplicate, 409 stale version, 400 malformed |
| POST   | `/v1/tick`     | Plans the available triggers; returns 0–20 actions (placeholder message text until Phase 3) |
| POST   | `/v1/reply`    | Records the turn, returns `{"action": "wait", "wait_seconds": 1800, ...}` |

Interactive schema: `http://localhost:8080/docs`. Example calls:

```bash
curl localhost:8080/v1/healthz
curl -X POST localhost:8080/v1/context -H 'Content-Type: application/json' -d '{
  "scope": "trigger", "context_id": "trg_001", "version": 1, "delivered_at": "2026-04-26T10:32:00Z",
  "payload": {"id": "trg_001", "scope": "merchant", "kind": "research_digest", "merchant_id": "m_001"}}'
curl -X POST localhost:8080/v1/tick -H 'Content-Type: application/json' \
  -d '{"now": "2026-04-26T10:35:00Z", "available_triggers": ["trg_001"]}'
curl -X POST localhost:8080/v1/reply -H 'Content-Type: application/json' -d '{
  "conversation_id": "conv_001", "merchant_id": "m_001", "from_role": "merchant",
  "message": "Yes please", "received_at": "2026-04-26T10:42:00Z", "turn_number": 2}'
```

See [`docs/architecture.md`](docs/architecture.md) for contract details and deliberate deviations
from the official examples (notably: a same-version context re-post returns 200, not 409).
