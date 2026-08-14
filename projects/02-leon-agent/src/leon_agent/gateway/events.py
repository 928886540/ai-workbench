"""SSE event schema and per-session event bus."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LeonEvent:
    event: str
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now_iso)

    def to_sse(self) -> str:
        payload = json.dumps(
            {
                "event": self.event,
                "session_id": self.session_id,
                "timestamp": self.timestamp,
                "data": self.data,
            },
            ensure_ascii=False,
        )
        return f"data: {payload}\n\n"


class SessionEventBus:
    """Thread-safe per-session fan-out event bus.

    on_event callbacks fire from a worker thread (asyncio.to_thread).
    SSE consumers live in the async event loop.
    We bridge via loop.call_soon_threadsafe.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queues: list[asyncio.Queue[LeonEvent | None]] = []

    def subscribe(self) -> asyncio.Queue[LeonEvent | None]:
        q: asyncio.Queue[LeonEvent | None] = asyncio.Queue()
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[LeonEvent | None]) -> None:
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    def publish(self, event: LeonEvent) -> None:
        """Safe to call from any thread."""
        for q in list(self._queues):
            self._loop.call_soon_threadsafe(q.put_nowait, event)

    def publish_done(self) -> None:
        """Signal all SSE consumers that the agent run is finished (None sentinel)."""
        for q in list(self._queues):
            self._loop.call_soon_threadsafe(q.put_nowait, None)


class EventBusRegistry:
    """Process-global registry mapping session_id -> SessionEventBus."""

    def __init__(self) -> None:
        self._buses: dict[str, SessionEventBus] = {}

    def get_or_create(
        self, session_id: str, loop: asyncio.AbstractEventLoop
    ) -> SessionEventBus:
        if session_id not in self._buses:
            self._buses[session_id] = SessionEventBus(loop)
        return self._buses[session_id]

    def get(self, session_id: str) -> SessionEventBus | None:
        return self._buses.get(session_id)
