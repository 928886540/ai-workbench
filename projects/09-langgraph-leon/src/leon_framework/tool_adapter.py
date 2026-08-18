"""Thin adapter from Leon's canonical ToolRegistry to LangChain tools."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from workbench_core.agent import ToolRegistry


def adapt_leon_tools(
    registry: ToolRegistry,
    names: Iterable[str] | None = None,
) -> list[BaseTool]:
    """Expose existing schemas and handlers without copying business tools."""

    specs = {
        str(schema["function"]["name"]): schema["function"]
        for schema in registry.schemas
    }
    selected = list(registry.names if names is None else names)
    unknown = [name for name in selected if name not in specs]
    if unknown:
        raise ValueError(f"Unknown Leon tools: {', '.join(unknown)}")

    tools: list[BaseTool] = []
    for name in selected:
        spec = specs[name]

        def execute(_tool_name: str = name, **arguments: Any) -> dict[str, Any]:
            return registry.execute(_tool_name, arguments)

        tools.append(
            StructuredTool.from_function(
                func=execute,
                name=name,
                description=str(spec["description"]),
                args_schema=deepcopy(spec["parameters"]),
                infer_schema=False,
            )
        )
    return tools
