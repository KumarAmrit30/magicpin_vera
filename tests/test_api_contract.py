import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.enums import ContextScope, ConversationState, TurnRole
from app.state.container import StateContainer
from tests.conftest import (
    SAMPLE_PAYLOADS,
    T0,
    FakeClock,
    context_body,
    customer_payload,
    load_seed_dataset,
    merchant_payload,
    reply_body,
    requires_dataset,
    trigger_payload,
)

STORED_AT_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
MERCHANT_ID = "m_001_drmeera_dentist_delhi"


def push_merchant(client: TestClient, version: int, views: int = 2410) -> Any:
    return client.post("/v1/context", json=context_body("merchant", MERCHANT_ID, version, merchant_payload(views=views)))


# --------------------------------------------------------------------------- #
# POST /v1/context — accepted
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scope", list(SAMPLE_PAYLOADS))
def test_context_accepts_every_scope(client: TestClient, state: StateContainer, scope: str) -> None:
    context_id, make_payload = SAMPLE_PAYLOADS[scope]

    response = client.post("/v1/context", json=context_body(scope, context_id, 1, make_payload()))

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "accepted": True,
        "ack_id": f"ack_{scope}_{context_id}_v1",
        "stored_at": "2026-04-26T10:00:00.000Z",
        "outcome": "created",
    }
    assert STORED_AT_FORMAT.match(body["stored_at"])
    assert state.context_store.exists(ContextScope(scope), context_id)


def test_context_preserves_payload_exactly(client: TestClient, state: StateContainer) -> None:
    payload = merchant_payload()
    payload["unmodeled_field"] = {"nested": [1, "two", {"three": None}]}

    client.post("/v1/context", json=context_body("merchant", MERCHANT_ID, 1, payload))

    assert state.context_store.get(ContextScope.MERCHANT, MERCHANT_ID).payload == payload


def test_context_same_version_is_idempotent_200(client: TestClient, state: StateContainer, clock: FakeClock) -> None:
    first = push_merchant(client, 1, views=2410).json()
    clock.advance(30)

    repeat = push_merchant(client, 1, views=9999)

    assert repeat.status_code == 200
    assert repeat.json() == {**first, "outcome": "duplicate"}
    assert state.context_store.get(ContextScope.MERCHANT, MERCHANT_ID).payload["performance"]["views"] == 2410
    assert client.get("/v1/healthz").json()["contexts_loaded"]["merchant"] == 1


def test_context_higher_version_replaces(client: TestClient, state: StateContainer, clock: FakeClock) -> None:
    push_merchant(client, 1, views=2410)
    clock.advance(45 * 60)

    response = push_merchant(client, 2, views=2580)

    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "ack_id": f"ack_merchant_{MERCHANT_ID}_v2",
        "stored_at": "2026-04-26T10:45:00.000Z",
        "outcome": "replaced",
    }
    record = state.context_store.get(ContextScope.MERCHANT, MERCHANT_ID)
    assert (record.version, record.payload["performance"]["views"]) == (2, 2580)


def test_context_lower_version_is_409_and_not_stored(client: TestClient, state: StateContainer) -> None:
    push_merchant(client, 5, views=5000)

    response = push_merchant(client, 4, views=4000)

    assert response.status_code == 409
    assert response.json() == {"accepted": False, "reason": "stale_version", "current_version": 5}
    record = state.context_store.get(ContextScope.MERCHANT, MERCHANT_ID)
    assert (record.version, record.payload["performance"]["views"]) == (5, 5000)


def test_context_versioning_sequence_over_http(client: TestClient, state: StateContainer) -> None:
    results = [(r.status_code, r.json().get("outcome")) for r in (push_merchant(client, v, views=v) for v in (1, 1, 2, 1))]

    assert results == [(200, "created"), (200, "duplicate"), (200, "replaced"), (409, None)]
    assert state.context_store.get(ContextScope.MERCHANT, MERCHANT_ID).payload["performance"]["views"] == 2


# --------------------------------------------------------------------------- #
# POST /v1/context — rejected as malformed (400)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"scope": "planet"}, "invalid_scope"),
        ({"context_id": ""}, "invalid_context_id"),
        ({"version": 0}, "invalid_version"),
        ({"version": -1}, "invalid_version"),
        ({"version": 1.5}, "invalid_version"),
        ({"version": "3"}, "invalid_version"),
        ({"version": True}, "invalid_version"),
        ({"delivered_at": "2026-04-26T10:00:00"}, "invalid_delivered_at"),
        ({"delivered_at": "yesterday"}, "invalid_delivered_at"),
        ({"payload": ["not", "an", "object"]}, "invalid_payload"),
    ],
)
def test_context_malformed_envelope_is_400(client: TestClient, override: dict[str, Any], reason: str) -> None:
    body = {**context_body("merchant", MERCHANT_ID, 1, merchant_payload()), **override}

    response = client.post("/v1/context", json=body)

    assert response.status_code == 400
    assert response.json()["accepted"] is False
    assert response.json()["reason"] == reason
    assert response.json()["details"]


@pytest.mark.parametrize("missing", ["scope", "context_id", "version", "payload", "delivered_at"])
def test_context_missing_field_is_400(client: TestClient, missing: str) -> None:
    body = context_body("merchant", MERCHANT_ID, 1, merchant_payload())
    del body[missing]

    response = client.post("/v1/context", json=body)

    assert response.status_code == 400
    assert missing in response.json()["details"]


def test_context_non_json_body_is_400(client: TestClient) -> None:
    response = client.post("/v1/context", content=b"{not json", headers={"Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.json()["reason"] == "invalid_request"


@pytest.mark.parametrize(
    ("scope", "context_id", "payload"),
    [
        ("merchant", MERCHANT_ID, {k: v for k, v in merchant_payload().items() if k != "merchant_id"}),
        ("merchant", MERCHANT_ID, {**merchant_payload(), "signals": "not-a-list"}),
        ("customer", "c_001_priya_for_m001", {**customer_payload(), "state": "sleeping"}),
        ("trigger", "trg_001_research_digest_dentists", {**trigger_payload(), "urgency": 9}),
        ("trigger", "trg_001_research_digest_dentists", {**trigger_payload(), "scope": "planet"}),
        ("category", "dentists", {"display_name": "Dentists"}),
    ],
)
def test_context_payload_not_matching_scope_schema_is_400(
    client: TestClient, state: StateContainer, scope: str, context_id: str, payload: dict[str, Any]
) -> None:
    response = client.post("/v1/context", json=context_body(scope, context_id, 1, payload))

    assert response.status_code == 400
    assert response.json()["reason"] == "invalid_payload"
    assert response.json()["details"].startswith("payload.")
    assert not state.context_store.exists(ContextScope(scope), context_id)


def test_context_rejection_does_not_affect_stored_version(client: TestClient, state: StateContainer) -> None:
    push_merchant(client, 1)

    client.post("/v1/context", json=context_body("merchant", MERCHANT_ID, 2, {"category_slug": "dentists"}))

    assert state.context_store.get(ContextScope.MERCHANT, MERCHANT_ID).version == 1


@requires_dataset
def test_context_warmup_with_official_seed_dataset(client: TestClient) -> None:
    dataset = load_seed_dataset()

    for scope, items in dataset.items():
        for context_id, payload in items:
            response = client.post("/v1/context", json=context_body(scope, context_id, 1, payload))
            assert response.status_code == 200, (scope, context_id, response.json())
            assert response.json()["outcome"] == "created"

    loaded = client.get("/v1/healthz").json()["contexts_loaded"]
    assert loaded == {scope: len(items) for scope, items in dataset.items()}


# --------------------------------------------------------------------------- #
# POST /v1/tick
# --------------------------------------------------------------------------- #


def test_tick_returns_empty_actions(client: TestClient) -> None:
    client.post("/v1/context", json=context_body("trigger", "trg_001_research_digest_dentists", 1, trigger_payload()))

    response = client.post(
        "/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": ["trg_001_research_digest_dentists"]}
    )

    assert response.status_code == 200
    assert response.json() == {"actions": []}


def test_tick_available_triggers_defaults_to_empty(client: TestClient) -> None:
    response = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z"})

    assert response.status_code == 200
    assert response.json() == {"actions": []}


def test_tick_does_not_mutate_state(client: TestClient, state: StateContainer) -> None:
    push_merchant(client, 1)
    before = (state.context_store.counts(), len(state.conversation_store), len(state.suppression_store))

    client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": ["unknown_trigger"]})

    assert (state.context_store.counts(), len(state.conversation_store), len(state.suppression_store)) == before


TICK_ACTION_KEYS = {
    "conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
    "template_params", "body", "cta", "suppression_key", "rationale",
}


def push_seed(client: TestClient) -> None:
    for scope, items in load_seed_dataset().items():
        for context_id, payload in items:
            assert client.post("/v1/context", json=context_body(scope, context_id, 1, payload)).status_code == 200


@requires_dataset
def test_tick_emits_contract_shaped_actions(client: TestClient, state: StateContainer) -> None:
    push_seed(client)

    response = client.post(
        "/v1/tick",
        json={"now": "2026-04-26T04:30:00Z", "available_triggers": ["trg_001_research_digest_dentists", "trg_003_recall_due_priya"]},
    )

    assert response.status_code == 200
    actions = response.json()["actions"]
    assert len(actions) == 2
    assert all(set(action) == TICK_ACTION_KEYS for action in actions)
    assert {(a["trigger_id"], a["send_as"], a["customer_id"]) for a in actions} == {
        ("trg_001_research_digest_dentists", "vera", None),
        ("trg_003_recall_due_priya", "merchant_on_behalf", "c_001_priya_for_m001"),
    }
    assert all(state.conversation_store.exists(a["conversation_id"]) for a in actions)


@requires_dataset
def test_repeated_tick_does_not_resend(client: TestClient) -> None:
    push_seed(client)
    body = {"now": "2026-04-26T04:30:00Z", "available_triggers": ["trg_001_research_digest_dentists"]}

    assert len(client.post("/v1/tick", json=body).json()["actions"]) == 1
    assert client.post("/v1/tick", json=body).json() == {"actions": []}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"now": "not-a-time"},
        {"now": "2026-04-26T10:35:00"},
        {"now": "2026-04-26T10:35:00Z", "available_triggers": "trg_001"},
    ],
)
def test_tick_invalid_payload_is_422(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/v1/tick", json=body).status_code == 422


# --------------------------------------------------------------------------- #
# POST /v1/reply
# --------------------------------------------------------------------------- #

SEND_KEYS = {"action", "body", "cta", "rationale"}


def test_reply_on_unknown_conversation_creates_it(client: TestClient, state: StateContainer) -> None:
    response = client.post("/v1/reply", json=reply_body("conv_001"))

    assert response.status_code == 200
    assert set(response.json()) == SEND_KEYS
    assert response.json()["cta"] == "binary_confirm_cancel"
    conversation = state.conversation_store.get("conv_001")
    assert conversation.merchant_id == MERCHANT_ID
    assert conversation.state is ConversationState.COMMITTED
    merchant_turn, vera_turn = conversation.turns
    assert (merchant_turn.role, merchant_turn.body, merchant_turn.turn_number) == (
        TurnRole.MERCHANT, "Yes please send the abstract.", 2
    )
    assert (vera_turn.role, vera_turn.body) == (TurnRole.VERA, response.json()["body"])


def test_reply_appends_turns_in_order_and_preserves_state(client: TestClient, state: StateContainer, clock: FakeClock) -> None:
    client.post("/v1/reply", json=reply_body("conv_001", message="first", turn_number=2))
    created_at = state.conversation_store.get("conv_001").created_at
    clock.advance(60)

    response = client.post("/v1/reply", json=reply_body("conv_001", message="second", turn_number=3))

    assert response.json()["action"] == "send"
    conversation = state.conversation_store.get("conv_001")
    assert [(t.role, t.body) for t in conversation.turns[::2]] == [(TurnRole.MERCHANT, "first"), (TurnRole.MERCHANT, "second")]
    assert [t.role for t in conversation.turns[1::2]] == [TurnRole.VERA, TurnRole.VERA]
    assert conversation.state is ConversationState.QUALIFYING
    assert conversation.created_at == created_at
    assert conversation.updated_at == clock.now


def test_reply_does_not_regress_committed_state(client: TestClient, state: StateContainer) -> None:
    state.conversation_store.create("conv_c", merchant_id=MERCHANT_ID, state=ConversationState.COMMITTED)

    response = client.post("/v1/reply", json=reply_body("conv_c", message="How long does it take?"))

    assert response.json()["action"] == "send"
    assert state.conversation_store.get("conv_c").state is ConversationState.COMMITTED


@pytest.mark.parametrize("terminal", [ConversationState.ENDED, ConversationState.COMPLETED])
def test_reply_on_closed_conversation_returns_end(
    client: TestClient, state: StateContainer, terminal: ConversationState
) -> None:
    state.conversation_store.create("conv_x", merchant_id=MERCHANT_ID, state=terminal)

    response = client.post("/v1/reply", json=reply_body("conv_x"))

    assert response.status_code == 200
    assert response.json()["action"] == "end"
    assert set(response.json()) == {"action", "rationale"}
    conversation = state.conversation_store.get("conv_x")
    assert conversation.state is terminal
    assert len(conversation.turns) == 1


def test_reply_without_merchant_id_is_accepted_and_backfilled_later(client: TestClient, state: StateContainer) -> None:
    body = reply_body("conv_auto")
    del body["merchant_id"], body["customer_id"]

    assert client.post("/v1/reply", json=body).status_code == 200
    assert state.conversation_store.get("conv_auto").merchant_id is None

    client.post("/v1/reply", json=reply_body("conv_auto", turn_number=3))
    assert state.conversation_store.get("conv_auto").merchant_id == MERCHANT_ID


def test_reply_from_customer(client: TestClient, state: StateContainer) -> None:
    body = reply_body("conv_priya", from_role="customer", customer_id="c_001_priya_for_m001", message="1")

    assert client.post("/v1/reply", json=body).status_code == 200
    conversation = state.conversation_store.get("conv_priya")
    assert conversation.customer_id == "c_001_priya_for_m001"
    assert conversation.turns[0].role is TurnRole.CUSTOMER


def test_reply_for_another_merchants_conversation_is_refused(client: TestClient, state: StateContainer) -> None:
    state.conversation_store.create("conv_m1", merchant_id=MERCHANT_ID)

    response = client.post("/v1/reply", json=reply_body("conv_m1", merchant_id="m_002_other"))

    assert response.status_code == 200
    assert set(response.json()) == {"action", "rationale"} and response.json()["action"] == "end"
    assert state.conversation_store.get("conv_m1").turns == []


def test_hostile_reply_ends_and_suppresses_merchant(client: TestClient, state: StateContainer) -> None:
    response = client.post("/v1/reply", json=reply_body("conv_h", message="Stop messaging me. This is useless spam."))

    assert response.json()["action"] == "end"
    assert state.suppression_store.is_suppressed(f"suppress:merchant:{MERCHANT_ID}", T0)
    assert state.conversation_store.get("conv_h").state is ConversationState.ENDED


@pytest.mark.parametrize(
    "override",
    [
        {"from_role": "vera"},
        {"turn_number": 0},
        {"turn_number": "2"},
        {"conversation_id": ""},
        {"received_at": "2026-04-26T10:42:00"},
        {"message": None},
    ],
)
def test_reply_invalid_payload_is_422(client: TestClient, state: StateContainer, override: dict[str, Any]) -> None:
    response = client.post("/v1/reply", json={**reply_body("conv_bad"), **override})

    assert response.status_code == 422
    assert len(state.conversation_store) == 0


@pytest.mark.parametrize("missing", ["conversation_id", "from_role", "message", "received_at", "turn_number"])
def test_reply_missing_required_field_is_422(client: TestClient, missing: str) -> None:
    body = reply_body("conv_bad")
    del body[missing]

    assert client.post("/v1/reply", json=body).status_code == 422


def test_judge_simulator_style_replies_on_unseen_conversations(client: TestClient) -> None:
    for i in range(1, 5):
        body = reply_body(f"conv_auto_{i}", message="Thank you for contacting us! Our team will respond shortly.",
                          turn_number=i + 1)
        response = client.post("/v1/reply", json=body)
        assert response.status_code == 200
        assert response.json()["action"] in {"send", "wait", "end"}


# --------------------------------------------------------------------------- #
# Determinism + schema
# --------------------------------------------------------------------------- #


def run_session(clock: FakeClock) -> list[tuple[int, Any]]:
    app = create_app(settings=Settings(), state=StateContainer.create(clock=clock, monotonic=clock.monotonic))
    client = TestClient(app)
    requests = [
        ("GET", "/v1/healthz", None),
        ("GET", "/v1/metadata", None),
        ("POST", "/v1/context", context_body("merchant", MERCHANT_ID, 1, merchant_payload())),
        ("POST", "/v1/context", context_body("merchant", MERCHANT_ID, 1, merchant_payload())),
        ("POST", "/v1/context", context_body("merchant", MERCHANT_ID, 2, merchant_payload(views=2580))),
        ("POST", "/v1/context", context_body("merchant", MERCHANT_ID, 1, merchant_payload())),
        ("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": []}),
        ("POST", "/v1/reply", reply_body("conv_001")),
        ("POST", "/v1/reply", reply_body("conv_001", turn_number=3)),
        ("GET", "/v1/healthz", None),
    ]
    return [(r.status_code, r.json()) for r in (client.request(m, p, json=b) for m, p, b in requests)]


def test_identical_request_sequences_produce_identical_responses() -> None:
    assert run_session(FakeClock()) == run_session(FakeClock())


def test_openapi_exposes_exactly_the_five_endpoints(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert {(path, method) for path, ops in schema["paths"].items() for method in ops} == {
        ("/v1/healthz", "get"),
        ("/v1/metadata", "get"),
        ("/v1/context", "post"),
        ("/v1/tick", "post"),
        ("/v1/reply", "post"),
    }
    assert {"200", "400", "409"} <= set(schema["paths"]["/v1/context"]["post"]["responses"])
