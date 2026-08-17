"""Tool definitions and resilient execution boundaries."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import CancelledError
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

ToolHandler = Callable[..., dict[str, Any]]
DirectAnswerFormatter = Callable[[dict[str, Any], dict[str, Any]], str | None]
AuditProjection = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    return_direct: bool = False
    answer_formatter: DirectAnswerFormatter | None = None
    audit_arguments: AuditProjection | None = None
    audit_result: AuditProjection | None = None
    span_kind: Literal["tool", "planning"] = "tool"

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Own the model-visible schemas and their local handlers."""

    def __init__(self, tools: Iterable[AgentTool] = ()) -> None:
        self._tools: dict[str, AgentTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AgentTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema for tool in self._tools.values()]

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def audit_name(self, name: str) -> str:
        """Return a persistable name without echoing an unknown model-supplied value."""

        return name if name in self._tools else "unknown_tool"

    def span_kind(self, name: str) -> Literal["tool", "planning"]:
        """Return a stable trace kind without trusting a model-supplied tool name."""

        tool = self._tools.get(name)
        return tool.span_kind if tool is not None else "tool"

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        try:
            return tool.handler(**arguments)
        except CancelledError:
            # Cancellation is control flow owned by AgentRuntime. Converting it
            # into a tool error would emit a misleading tool_finished event and
            # delay the turn from stopping until the next runtime boundary.
            raise
        except TypeError as exc:
            return {"ok": False, "error": f"Invalid arguments for {name}: {exc}"}
        except Exception as exc:  # noqa: BLE001 - tools are an external execution boundary
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _project(
        projection: AuditProjection | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply an audit projection without exposing the live/raw payload on failure."""

        try:
            detached = deepcopy(payload)
            projected = projection(detached) if projection is not None else detached
            if not isinstance(projected, dict):
                raise TypeError("audit projection must return a dict")
            return deepcopy(projected)
        except Exception:  # noqa: BLE001 - audit output must fail closed
            return {"audit_error": "projection_failed"}

    def audit_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return the event/audit view of one tool's arguments."""

        tool = self._tools.get(name)
        if tool is None:
            return {"audit_error": "unknown_tool"}
        return self._project(tool.audit_arguments, arguments)

    def audit_result(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        """Return the event/audit view of one tool's result."""

        tool = self._tools.get(name)
        if tool is None:
            return {"audit_error": "unknown_tool"}
        return self._project(tool.audit_result, result)

    def direct_answer(
        self,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> str | None:
        """Return a tool-owned answer when the model need not be called again."""
        tool = self._tools.get(name)
        if tool is None or not tool.return_direct:
            return None
        if tool.answer_formatter is not None:
            answer = tool.answer_formatter(arguments, result)
        else:
            answer = result.get("answer")
        if answer is None:
            return None
        return str(answer).strip() or None
