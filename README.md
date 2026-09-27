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

**Decision intelligence is not implemented yet.** `/v1/tick` always returns
`{"actions": []}`, and `/v1/reply` records the message and answers `wait`
(or `end` for an already-closed conversation). No messages are composed.

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

Not yet implemented:

- winner selection
- planner integration (`/v1/tick`, `/v1/reply`) and writing suppression on send
- message composition

Design principle: *triggers are evidence, not instructions.* See the "Decision Domain — Phase 2A",
"Candidate Generation — Phase 2B" and "Eligibility & Suppression — Phase 2C" sections of
[`docs/architecture.md`](docs/architecture.md), and [`docs/phase-2c-eligibility.md`](docs/phase-2c-eligibility.md).

## Architecture

```text
app/
├── main.py            create_app(): owns settings + StateContainer, mounts routers under /v1
├── config.py          Settings (VERA_* env vars), logging setup
├── clock.py           injectable UTC clock
├── api/               health.py (healthz, metadata), context.py, tick.py, reply.py, deps.py
├── models/            enums.py, schemas.py (API contract), domain.py (context payloads)
├── engine/            decision domain: archetypes, actions, evidence, features, scoring, plans, eligibility (no FastAPI)
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

| Method | Path           | Phase 1A behavior |
|--------|----------------|-------------------|
| GET    | `/v1/healthz`  | `{"status": "ok", "uptime_seconds": N, "contexts_loaded": {...}}` |
| GET    | `/v1/metadata` | Bot identity (team fields from env, `engine: "deterministic"`, `model: "none"`) |
| POST   | `/v1/context`  | 200 created/replaced/duplicate, 409 stale version, 400 malformed |
| POST   | `/v1/tick`     | Validates input, returns `{"actions": []}` |
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
