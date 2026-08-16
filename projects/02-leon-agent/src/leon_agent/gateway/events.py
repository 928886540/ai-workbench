"""SSE event schema and per-session event bus."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock, RLock
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_event_id_lock = Lock()
_next_event_id_value = 0


def _next_event_id() -> int:
    """Return a process-local, monotonically increasing SSE event id."""
    global _next_event_id_value
    with _event_id_lock:
        _next_event_id_value += 1
        return _next_event_id_value


def _event_id_value(value: int | str | None) -> int | None:
    """Normalize a Last-Event-ID value without making malformed cursors fatal."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class LeonEvent:
    event: str
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now_iso)
    # The bus assigns the cursor at publish time so concurrent producers retain
    # publish order. Synthetic transport-only events may stay data-only.
    id: int | None = None

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
        id_line = f"id: {self.id}\n" if self.id is not None else ""
        return f"{id_line}data: {payload}\n\n"


class SessionEventBus:
    """Thread-safe per-session fan-out event bus.

    on_event callbacks fire from a worker thread (asyncio.to_thread).
    SSE consumers live in the async event loop.
    We bridge via loop.call_soon_threadsafe.
    """

    HISTORY_LIMIT = 100

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        history_limit: int = HISTORY_LIMIT,
    ) -> None:
        if history_limit <= 0:
            raise ValueError("history_limit must be positive")
        self._loop = loop
        self._lock = RLock()
        self._queues: list[asyncio.Queue[LeonEvent | None]] = []
        self._history: deque[LeonEvent] = deque(maxlen=history_limit)

    def subscribe(
        self, last_event_id: int | str | None = None
    ) -> asyncio.Queue[LeonEvent | None]:
        """Subscribe and enqueue retained events newer than ``last_event_id``.

        A missing cursor starts a fresh stream and intentionally does not replay
        retained events.  If a cursor predates the in-memory window, all events
        still retained are returned; the caller can detect the gap from the
        first returned id if durable replay is ever added.
        """
        q: asyncio.Queue[LeonEvent | None] = asyncio.Queue()
        cursor = _event_id_value(last_event_id)
        with self._lock:
            self._queues.append(q)
            replay = (
                [
                    event
                    for event in self._history
                    if event.id is not None and event.id > cursor
                ]
                if cursor is not None
                else []
            )
            # Queue replay callbacks while holding the same lock used by
            # publish(), so a concurrent publisher cannot overtake the replay.
            for event in replay:
                self._enqueue(q, event)
        return q

    def unsubscribe(self, q: asyncio.Queue[LeonEvent | None]) -> None:
        with self._lock:
            try:
                self._queues.remove(q)
            except ValueError:
                pass

    def _enqueue(self, q: asyncio.Queue[LeonEvent | None], value: LeonEvent | None) -> None:
        """Put a value on a queue from either the event-loop or a worker thread."""
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self._loop:
            q.put_nowait(value)
        else:
            self._loop.call_soon_threadsafe(q.put_nowait, value)

    def publish(self, event: LeonEvent) -> None:
        """Safe to call from any thread."""
        with self._lock:
            if event.id is None:
                event.id = _next_event_id()
            self._history.append(event)
            queues = list(self._queues)
            for q in queues:
                self._enqueue(q, event)

    def publish_done(self) -> None:
        """Signal all SSE consumers that the agent run is finished (None sentinel)."""
        with self._lock:
            queues = list(self._queues)
            for q in queues:
                self._enqueue(q, None)


class EventBusRegistry:
    """Process-global registry mapping session_id -> SessionEventBus."""

    def __init__(self) -> None:
        self._buses: dict[str, SessionEventBus] = {}
        self._lock = RLock()

    def get_or_create(
        self, session_id: str, loop: asyncio.AbstractEventLoop
    ) -> SessionEventBus:
        with self._lock:
            if session_id not in self._buses:
                self._buses[session_id] = SessionEventBus(loop)
            return self._buses[session_id]

    def get(self, session_id: str) -> SessionEventBus | None:
        with self._lock:
            return self._buses.get(session_id)
