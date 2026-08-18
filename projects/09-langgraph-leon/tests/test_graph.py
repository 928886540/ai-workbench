from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from leon_agent.file_tools import create_file_tools
from leon_agent.memory import MemoryService, MemoryStore, memory_turn
from leon_agent.memory.tools import create_memory_tools
from leon_framework.graph import build_leon_graph
from workbench_core.agent import AgentTool, ToolRegistry
from workbench_core.files import FileSearchService


class ScriptedFileModel:
    def __init__(self) -> None:
        self.bound_names: list[str] = []

    def bind_tools(self, tools: list[Any]) -> ScriptedFileModel:
        self.bound_names = [tool.name for tool in tools]
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        if tool_messages:
            return AIMessage(content="tool-observed")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {
                        "root_id": "workspace",
                        "relative_path": "sample.txt",
                        "start_line": 1,
                        "max_lines": 20,
                    },
                    "id": "read-1",
                    "type": "tool_call",
                }
            ],
        )


class DirectModel:
    def invoke(self, messages: list[Any]) -> AIMessage:
        return AIMessage(content=f"direct:{messages[-1].content}")


class PlanningAwareModel:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.messages = messages
        return AIMessage(content="planned-answer")


class ScriptedMemoryModel:
    def bind_tools(self, tools: list[Any]) -> ScriptedMemoryModel:
        assert [tool.name for tool in tools] == [
            "memory_get",
            "memory_upsert",
            "memory_delete",
        ]
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="memory-observed")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "memory_upsert",
                    "args": {
                        "key": "profile.style",
                        "value": {"value": "concise"},
                    },
                    "id": "memory-1",
                    "type": "tool_call",
                }
            ],
        )


def test_graph_executes_existing_leon_file_tool_and_checkpoints(tmp_path) -> None:
    (tmp_path / "sample.txt").write_text("shared tool proof\n", encoding="utf-8")
    registry = ToolRegistry(create_file_tools(FileSearchService({"workspace": tmp_path})))
    model = ScriptedFileModel()
    checkpointer = InMemorySaver()
    graph = build_leon_graph(
        model,
        registry,
        tool_names=["read_file"],
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": "test-thread"}}
    arguments = {
        "root_id": "workspace",
        "relative_path": "sample.txt",
        "start_line": 1,
        "max_lines": 20,
    }

    result = graph.invoke({"messages": [HumanMessage(content="读取示例文件")]}, config)

    assert model.bound_names == ["read_file"]
    tool_message = next(
        message for message in result["messages"] if isinstance(message, ToolMessage)
    )
    assert json.loads(tool_message.content) == registry.execute("read_file", arguments)
    assert result["messages"][-1].content == "tool-observed"
    assert graph.get_state(config).values["messages"][-1].content == "tool-observed"


def test_graph_supports_chat_when_no_optional_tools_are_configured() -> None:
    graph = build_leon_graph(DirectModel(), ToolRegistry())

    result = graph.invoke({"messages": [HumanMessage(content="hello")]})

    assert result["messages"][-1].content == "direct:hello"


def test_graph_stores_plan_and_injects_it_as_ephemeral_context() -> None:
    planner_calls: list[list[Any]] = []

    def planner(messages: list[Any]) -> list[str]:
        planner_calls.append(messages)
        return ["Inspect the request.", "Collect evidence.", "Answer from evidence."]

    model = PlanningAwareModel()
    checkpointer = InMemorySaver()
    graph = build_leon_graph(
        model,
        ToolRegistry(),
        planner=planner,
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": "planning-thread"}}

    result = graph.invoke(
        {"messages": [HumanMessage(content="分析这个项目")]},
        config,
    )

    assert len(planner_calls) == 1
    assert result["plan"] == [
        "Inspect the request.",
        "Collect evidence.",
        "Answer from evidence.",
    ]
    plan_context = next(
        message.content
        for message in model.messages
        if isinstance(message, SystemMessage)
    )
    assert "1. Inspect the request." in plan_context
    assert graph.get_state(config).values["plan"] == result["plan"]


def test_graph_does_not_inject_stale_plan_when_resumed_without_planning() -> None:
    checkpointer = InMemorySaver()
    config = {"configurable": {"thread_id": "planning-mode-change"}}
    planned_model = PlanningAwareModel()
    planned_graph = build_leon_graph(
        planned_model,
        ToolRegistry(),
        planner=lambda messages: ["FIRST_TURN_PLAN_A", "FIRST_TURN_PLAN_B"],
        checkpointer=checkpointer,
    )
    planned_graph.invoke(
        {"messages": [HumanMessage(content="first request")]},
        config,
    )

    resumed_model = PlanningAwareModel()
    resumed_graph = build_leon_graph(
        resumed_model,
        ToolRegistry(),
        checkpointer=checkpointer,
    )
    resumed_graph.invoke(
        {"messages": [HumanMessage(content="unrelated second request")]},
        config,
    )

    assert all(
        "FIRST_TURN_PLAN" not in str(message.content)
        for message in resumed_model.messages
        if isinstance(message, SystemMessage)
    )


def test_graph_injects_memory_context_without_checkpointing_raw_values(tmp_path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    store.upsert(
        "user",
        "local-owner",
        "profile.style",
        {"value": "concise"},
        source_kind="fixture",
        source_session_id="fixture",
        now_ms=1,
    )
    service = MemoryService(store, session_id="framework-thread")
    model = PlanningAwareModel()
    graph = build_leon_graph(
        model,
        ToolRegistry(),
        system_context_provider=service.build_context,
    )

    result = graph.invoke({"messages": [HumanMessage(content="分析这个项目")]})

    assert any(
        isinstance(message, SystemMessage) and "profile.style" in str(message.content)
        for message in model.messages
    )
    assert all("profile.style" not in str(message.content) for message in result["messages"])


def test_tool_node_preserves_memory_consent_context(tmp_path) -> None:
    service = MemoryService(
        MemoryStore(tmp_path / "memory.db"),
        session_id="framework-thread",
    )
    registry = ToolRegistry(create_memory_tools(service))
    graph = build_leon_graph(ScriptedMemoryModel(), registry)

    with memory_turn("remember this preference"):
        result = graph.invoke(
            {"messages": [HumanMessage(content="remember this preference")]}
        )

    tool_message = next(
        message for message in result["messages"] if isinstance(message, ToolMessage)
    )
    assert json.loads(tool_message.content)["ok"] is True
    assert service.get(key="profile.style")["count"] == 1


def test_interrupt_before_tools_resumes_without_repeating_the_tool() -> None:
    calls: list[dict[str, Any]] = []

    def read_file(**arguments: Any) -> dict[str, Any]:
        calls.append(arguments)
        return {"ok": True, "content": "checkpoint evidence"}

    registry = ToolRegistry(
        [
            AgentTool(
                name="read_file",
                description="Read one test file.",
                parameters={
                    "type": "object",
                    "properties": {
                        "root_id": {"type": "string"},
                        "relative_path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "max_lines": {"type": "integer"},
                    },
                    "required": ["root_id", "relative_path"],
                    "additionalProperties": False,
                },
                handler=read_file,
            )
        ]
    )
    checkpointer = InMemorySaver()
    graph = build_leon_graph(
        ScriptedFileModel(),
        registry,
        interrupt_before=["tools"],
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": "interrupt-thread"}}

    list(
        graph.stream(
            {"messages": [HumanMessage(content="读取示例文件")]},
            config,
            stream_mode="updates",
        )
    )

    assert calls == []
    assert graph.get_state(config).next == ("tools",)

    list(graph.stream(None, config, stream_mode="updates"))

    assert len(calls) == 1
    assert graph.get_state(config).next == ()
    assert graph.get_state(config).values["messages"][-1].content == "tool-observed"
