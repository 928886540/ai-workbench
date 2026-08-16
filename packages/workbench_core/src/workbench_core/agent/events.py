"""Data emitted by the shared agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolStep:
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


@dataclass(frozen=True)
class AgentEvent:
    kind: str
    turn: int = 0
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    content: str | None = None


@dataclass
class AgentResult:
    answer: str
    steps: list[ToolStep] = field(default_factory=list)
    turns: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    usage: dict[str, int] | None = None
