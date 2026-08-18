from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from leon_agent.file_tools import create_file_tools
from leon_framework.graph import build_leon_graph
from workbench_core.agent import ToolRegistry
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
