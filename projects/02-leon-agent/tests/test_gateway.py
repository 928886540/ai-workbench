"""Smoke tests for the Leon Agent HTTP Gateway."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from leon_agent.gateway.app import app
from leon_agent.models import MODEL_IDS


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Provide a TestClient with a temporary SQLite DB."""
    monkeypatch.setenv("LEON_SESSION_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("LEON_API_TOKEN", "")  # disable auth in tests
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_health_requires_configured_token(tmp_path, monkeypatch):
    monkeypatch.setenv("LEON_SESSION_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("LEON_API_TOKEN", "test-token")
    with TestClient(app, raise_server_exceptions=True) as client:
        assert client.get("/api/health").status_code == 401
        assert client.get(
            "/api/health", headers={"Authorization": "Bearer test-token"}
        ).status_code == 200


def test_sse_invalid_token_stops_eventsource_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("LEON_SESSION_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("LEON_API_TOKEN", "test-token")
    with TestClient(app, raise_server_exceptions=True) as client:
        session_id = client.post(
            "/api/agent/sessions", headers={"Authorization": "Bearer test-token"}
        ).json()["session_id"]
        response = client.get(
            f"/api/agent/sessions/{session_id}/events?token=stale-token"
        )
        assert response.status_code == 204


def test_web_manifest_and_icon_are_served(client):
    assert client.get("/manifest.json").status_code == 200
    assert client.get("/icon.svg").status_code == 200


def test_web_shell_disables_stale_pwa_cache(client):
    assert client.get("/").headers["cache-control"] == "no-cache, no-store, must-revalidate"
    sw = client.get("/sw.js")
    assert sw.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert sw.headers["service-worker-allowed"] == "/"


def test_create_and_list_session(client):
    r = client.post("/api/agent/sessions")
    assert r.status_code == 200
    data = r.json()
    assert "session_id" in data
    session_id = data["session_id"]

    r2 = client.get("/api/agent/sessions")
    assert r2.status_code == 200
    ids = [s["id"] for s in r2.json()["sessions"]]
    assert session_id in ids


def test_get_session_not_found(client):
    r = client.get("/api/agent/sessions/nonexistent")
    assert r.status_code == 404


def test_get_messages_empty(client):
    r = client.post("/api/agent/sessions")
    session_id = r.json()["session_id"]
    r2 = client.get(f"/api/agent/sessions/{session_id}/messages")
    assert r2.status_code == 200
    assert r2.json()["messages"] == []


def test_session_model_can_be_selected_and_reset(client, monkeypatch):
    class FakeSettings:
        profile = "toml:test-provider"
        active_model = "default-model"

    monkeypatch.setattr("leon_agent.gateway.app.get_settings", lambda: FakeSettings())
    session_id = client.post("/api/agent/sessions").json()["session_id"]

    initial = client.get(f"/api/agent/sessions/{session_id}/model")
    assert initial.status_code == 200
    assert initial.json()["active_model"] == "default-model"
    assert initial.json()["selected_model"] is None
    assert initial.json()["models"] == list(MODEL_IDS)

    selected = client.put(
        f"/api/agent/sessions/{session_id}/model",
        json={"model": "gpt-5.6-sol"},
    )
    assert selected.status_code == 200
    assert selected.json()["active_model"] == "gpt-5.6-sol"
    assert selected.json()["selected_model"] == "gpt-5.6-sol"

    reset = client.put(
        f"/api/agent/sessions/{session_id}/model",
        json={"model": None},
    )
    assert reset.status_code == 200
    assert reset.json()["active_model"] == "default-model"
    assert reset.json()["selected_model"] is None


def test_session_model_rejects_unknown_catalog_entry(client, monkeypatch):
    class FakeSettings:
        profile = "toml:test-provider"
        active_model = "default-model"

    monkeypatch.setattr("leon_agent.gateway.app.get_settings", lambda: FakeSettings())
    session_id = client.post("/api/agent/sessions").json()["session_id"]
    response = client.put(
        f"/api/agent/sessions/{session_id}/model",
        json={"model": "not-in-catalog"},
    )
    assert response.status_code == 422


def test_send_message_session_not_found(client):
    r = client.post(
        "/api/agent/sessions/ghost/messages",
        json={"content": "hello"},
    )
    assert r.status_code == 404
