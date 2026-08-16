"""Provider-free tests for SSE ids and the in-memory replay window."""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
from leon_agent.gateway.app import app
from leon_agent.gateway.events import LeonEvent, SessionEventBus


def _drain(queue: asyncio.Queue[LeonEvent | None]) -> list[LeonEvent | None]:
    values: list[LeonEvent | None] = []
    while not queue.empty():
        values.append(queue.get_nowait())
    return values


def test_leon_event_ids_are_incrementing_sse_fields() -> None:
    first = LeonEvent(event="test.first", session_id="session")
    second = LeonEvent(event="test.second", session_id="session")
    assert first.id is None
    assert second.id is None

    async def scenario() -> None:
        bus = SessionEventBus(asyncio.get_running_loop())
        bus.publish(first)
        bus.publish(second)

    asyncio.run(scenario())
    assert first.id is not None
    assert second.id == first.id + 1
    assert first.to_sse().startswith(f"id: {first.id}\ndata: ")
    payload = json.loads(first.to_sse().split("data: ", 1)[1])
    assert payload["event"] == "test.first"

    synthetic = LeonEvent(event="session.connected", session_id="session", id=None)
    assert synthetic.to_sse().startswith("data: ")


def test_subscribe_replays_only_events_after_cursor_and_none_is_fresh() -> None:
    async def scenario() -> tuple[
        list[LeonEvent | None], list[LeonEvent | None], list[int]
    ]:
        bus = SessionEventBus(asyncio.get_running_loop())
        events = [
            LeonEvent(event=f"test.{index}", session_id="session")
            for index in range(3)
        ]
        for event in events:
            bus.publish(event)

        replay = bus.subscribe(events[0].id)
        fresh = bus.subscribe()
        return _drain(replay), _drain(fresh), [event.id for event in events[1:]]

    replay, fresh, expected_ids = asyncio.run(scenario())
    assert [event.id for event in replay if event is not None] == expected_ids
    assert fresh == []


def test_old_cursor_returns_retained_window_only() -> None:
    async def scenario() -> tuple[list[LeonEvent | None], int, int]:
        bus = SessionEventBus(asyncio.get_running_loop())
        events = [
            LeonEvent(event=f"test.{index}", session_id="session")
            for index in range(101)
        ]
        for event in events:
            bus.publish(event)
        queue = bus.subscribe(events[0].id)
        return _drain(queue), events[-100].id, events[-1].id

    values, first_retained_id, last_id = asyncio.run(scenario())
    ids = [event.id for event in values if event is not None]
    assert len(ids) == 100
    assert ids[0] == first_retained_id
    assert ids[-1] == last_id


def test_sse_route_reads_header_then_query_cursor(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("LEON_SESSION_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("LEON_API_TOKEN", "")
    seen: list[int | str | None] = []

    def subscribe(self, last_event_id=None):  # noqa: ANN001
        seen.append(last_event_id)
        queue: asyncio.Queue[LeonEvent | None] = asyncio.Queue()
        queue.put_nowait(None)
        return queue

    monkeypatch.setattr("leon_agent.gateway.events.SessionEventBus.subscribe", subscribe)
    with TestClient(app, raise_server_exceptions=True) as client:
        session_id = client.post("/api/agent/sessions").json()["session_id"]

        header_response = client.get(
            f"/api/agent/sessions/{session_id}/events",
            headers={"Last-Event-ID": "41"},
        )
        query_response = client.get(
            f"/api/agent/sessions/{session_id}/events?last_event_id=42",
        )

    assert header_response.status_code == 200
    assert query_response.status_code == 200
    assert seen == ["41", "42"]
    assert header_response.text.startswith("data: ")
