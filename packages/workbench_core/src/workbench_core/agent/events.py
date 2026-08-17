"""Data emitted by the shared agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolStep:
    """Persistable tool audit data; arguments and result may be projected."""

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    trace_id: str | None = None
    span_id: str | None = None


@dataclass(frozen=True)
class AgentEvent:
    """Stream-safe runtime event; tool payloads use the same audit projection."""

    kind: str
    turn: int = 0
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    content: str | None = None
    trace_id: str | None = None
    turn_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None


@dataclass
class AgentResult:
    answer: str
    steps: list[ToolStep] = field(default_factory=list)
    turns: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    usage: dict[str, int] | None = None
    trace_id: str | None = None
    turn_id: str | None = None
