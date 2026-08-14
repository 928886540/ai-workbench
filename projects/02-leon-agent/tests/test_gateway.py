"""Smoke tests for the Leon Agent HTTP Gateway."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from leon_agent.gateway.app import app


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


def test_send_message_session_not_found(client):
    r = client.post(
        "/api/agent/sessions/ghost/messages",
        json={"content": "hello"},
    )
    assert r.status_code == 404
