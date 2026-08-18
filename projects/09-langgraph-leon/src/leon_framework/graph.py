"""Minimal agent <-> ToolNode loop using Leon's existing tool registry."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from workbench_core.agent import ToolRegistry

from leon_framework.tool_adapter import adapt_leon_tools


def build_leon_graph(
    model: Any,
    registry: ToolRegistry,
    *,
    tool_names: Iterable[str] | None = None,
    checkpointer: Any = None,
) -> Any:
    """Compile the smallest framework runtime around the shared Leon tools."""

    tools = adapt_leon_tools(registry, tool_names)
    bound_model = model.bind_tools(tools)

    def call_model(state: MessagesState) -> dict[str, list[Any]]:
        return {"messages": [bound_model.invoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", call_model)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer)
