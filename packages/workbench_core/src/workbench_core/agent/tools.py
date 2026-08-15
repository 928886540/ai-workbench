"""Tool definitions and resilient execution boundaries."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

ToolHandler = Callable[..., dict[str, Any]]
DirectAnswerFormatter = Callable[[dict[str, Any], dict[str, Any]], str | None]


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    return_direct: bool = False
    answer_formatter: DirectAnswerFormatter | None = None

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

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        try:
            return tool.handler(**arguments)
        except TypeError as exc:
            return {"ok": False, "error": f"Invalid arguments for {name}: {exc}"}
        except Exception as exc:  # noqa: BLE001 - tools are an external execution boundary
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

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
