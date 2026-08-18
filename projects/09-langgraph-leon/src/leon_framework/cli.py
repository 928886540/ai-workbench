"""Provider-free proof that LangGraph can execute an existing Leon file tool."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from leon_agent.file_tools import create_file_search_service, create_file_tools
from workbench_core.agent import ToolRegistry

from leon_framework.graph import build_leon_graph


class _DemoModel:
    def bind_tools(self, tools: list[Any]) -> _DemoModel:
        self._tool_names = [tool.name for tool in tools]
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="Existing Leon read_file tool completed successfully.")
        if "read_file" not in self._tool_names:
            raise RuntimeError("read_file was not bound")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {
                        "root_id": "workspace",
                        "relative_path": "README.md",
                        "start_line": 1,
                        "max_lines": 3,
                    },
                    "id": "demo-read",
                    "type": "tool_call",
                }
            ],
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Leon Agent Framework Edition.")
    parser.add_argument("--demo", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    service = create_file_search_service({"workspace": Path.cwd()})
    if service is None:
        raise RuntimeError("workspace file service is unavailable")
    registry = ToolRegistry(create_file_tools(service))
    checkpointer = InMemorySaver()
    graph = build_leon_graph(
        _DemoModel(),
        registry,
        tool_names=["read_file"],
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": "leon-graph-demo"}}

    print("START")
    for update in graph.stream(
        {"messages": [HumanMessage(content="读取仓库 README 的前三行")]},
        config,
        stream_mode="updates",
    ):
        for node_name in update:
            print(f"  -> {node_name}")
    print("END")
    snapshot = graph.get_state(config)
    final_message = snapshot.values["messages"][-1]
    print(final_message.content)
    return 0
