"""Minimal agent <-> ToolNode loop using Leon's existing tool registry."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from langchain_core.messages import SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from workbench_core.agent import ToolRegistry

from leon_framework.planning import Planner, format_plan_context, validate_plan_steps
from leon_framework.tool_adapter import adapt_leon_tools


class LeonGraphState(MessagesState):
    plan: list[str]


def build_leon_graph(
    model: Any,
    registry: ToolRegistry,
    *,
    tool_names: Iterable[str] | None = None,
    planner: Planner | None = None,
    system_context_provider: Callable[[], str | None] | None = None,
    interrupt_before: Iterable[str] | None = None,
    checkpointer: Any = None,
) -> Any:
    """Compile the smallest framework runtime around the shared Leon tools."""

    tools = adapt_leon_tools(registry, tool_names)
    bound_model = model.bind_tools(tools) if tools else model

    def create_plan(state: LeonGraphState) -> dict[str, list[str]]:
        if planner is None:  # pragma: no cover - the node is added only with a planner
            raise RuntimeError("planner is unavailable")
        return {"plan": validate_plan_steps(planner(state["messages"]))}

    def call_model(state: LeonGraphState) -> dict[str, list[Any]]:
        messages = list(state["messages"])
        contexts: list[SystemMessage] = []
        if system_context_provider is not None:
            system_context = system_context_provider()
            if system_context:
                contexts.append(SystemMessage(content=system_context))
        plan = (state.get("plan") or []) if planner is not None else []
        if plan:
            contexts.append(SystemMessage(content=format_plan_context(plan)))
        insert_at = 0
        while insert_at < len(messages) and isinstance(messages[insert_at], SystemMessage):
            insert_at += 1
        messages[insert_at:insert_at] = contexts
        return {"messages": [bound_model.invoke(messages)]}

    builder = StateGraph(LeonGraphState)
    builder.add_node("agent", call_model)
    if planner is not None:
        builder.add_node("plan", create_plan)
        builder.add_edge(START, "plan")
        builder.add_edge("plan", "agent")
    else:
        builder.add_edge(START, "agent")
    if tools:
        builder.add_node("tools", ToolNode(tools))
        builder.add_conditional_edges("agent", tools_condition)
        builder.add_edge("tools", "agent")
    else:
        builder.add_edge("agent", END)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before) if interrupt_before is not None else None,
    )
