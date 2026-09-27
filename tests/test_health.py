from fastapi.testclient import TestClient

from app.config import Settings
from tests.conftest import FakeClock, context_body, merchant_payload


def test_healthz_returns_ok_with_zero_counts_on_startup(client: TestClient) -> None:
    response = client.get("/v1/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "uptime_seconds": 0,
        "contexts_loaded": {"category": 0, "merchant": 0, "customer": 0, "trigger": 0},
    }


def test_healthz_reports_uptime(client: TestClient, clock: FakeClock) -> None:
    clock.advance(124.9)

    assert client.get("/v1/healthz").json()["uptime_seconds"] == 124


def test_healthz_counts_loaded_contexts_per_scope(client: TestClient) -> None:
    for i in range(3):
        mid = f"m_{i}"
        client.post("/v1/context", json=context_body("merchant", mid, 1, merchant_payload(mid)))

    counts = client.get("/v1/healthz").json()["contexts_loaded"]

    assert counts == {"category": 0, "merchant": 3, "customer": 0, "trigger": 0}


def test_metadata_returns_contract_fields(client: TestClient) -> None:
    response = client.get("/v1/metadata")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at",
        "name", "engine", "description",
    }
    assert body["name"] == "Vera"
    assert body["version"] == "0.1.0"
    assert body["engine"] == "deterministic"
    assert body["model"] == "none"
    assert "not yet enabled" in body["approach"]


def test_metadata_is_stable_across_calls(client: TestClient) -> None:
    assert client.get("/v1/metadata").json() == client.get("/v1/metadata").json()


def test_settings_read_team_identity_from_env(monkeypatch) -> None:
    monkeypatch.setenv("VERA_TEAM_NAME", "Team Alpha")
    monkeypatch.setenv("VERA_TEAM_MEMBERS", "Alice, Bob,")
    monkeypatch.setenv("VERA_CONTACT_EMAIL", "team@example.com")
    monkeypatch.setenv("VERA_SUBMITTED_AT", "2026-04-26T08:00:00Z")

    settings = Settings.from_env()

    assert settings.team_name == "Team Alpha"
    assert settings.team_members == ("Alice", "Bob")
    assert settings.contact_email == "team@example.com"
    assert settings.submitted_at == "2026-04-26T08:00:00Z"
