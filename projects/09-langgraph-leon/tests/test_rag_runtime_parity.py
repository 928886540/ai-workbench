"""Prove one canonical RAG Tool keeps its observation across both runtimes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from leon_framework.graph import build_leon_graph
from rag_lab import (
    RAGSearchService,
    RetrievalHit,
    TextDocument,
    VectorRetriever,
    chunk_document,
    create_rag_search_tool,
    embed_chunks,
)
from rag_lab.providers import DeterministicFakeEmbeddingProvider
from workbench_core.agent import AgentRuntime, ToolRegistry
from workbench_core.llm import ChatTurn, ToolCall


class CountingRetriever:
    def __init__(self, delegate: VectorRetriever) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, int]] = []

    def retrieve(self, query: str, *, top_k: int) -> Sequence[RetrievalHit]:
        self.calls.append((query, top_k))
        return self.delegate.retrieve(query, top_k=top_k)


class SelfBuiltRAGClient:
    def __init__(self, arguments: dict[str, Any]) -> None:
        self.arguments = arguments
        self.calls = 0
        self.raw_observation: str | None = None
        self.observation: dict[str, Any] | None = None

    def chat_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> ChatTurn:
        self.calls += 1
        if self.calls == 1:
            assert tools is not None
            assert [tool["function"]["name"] for tool in tools] == ["rag_search"]
            arguments = json.dumps(self.arguments, ensure_ascii=False)
            return ChatTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="self-rag-1",
                        name="rag_search",
                        arguments=arguments,
                    )
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "self-rag-1",
                            "type": "function",
                            "function": {
                                "name": "rag_search",
                                "arguments": arguments,
                            },
                        }
                    ],
                },
            )
        tool_message = next(message for message in messages if message["role"] == "tool")
        self.raw_observation = str(tool_message["content"])
        self.observation = json.loads(self.raw_observation)
        return ChatTurn(
            content="self-built observed RAG evidence",
            raw_message={
                "role": "assistant",
                "content": "self-built observed RAG evidence",
            },
        )


class LangGraphRAGModel:
    def __init__(self, arguments: dict[str, Any]) -> None:
        self.arguments = arguments
        self.raw_observation: str | None = None
        self.observation: dict[str, Any] | None = None

    def bind_tools(self, tools: list[Any]) -> LangGraphRAGModel:
        assert [tool.name for tool in tools] == ["rag_search"]
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        if tool_messages:
            self.raw_observation = str(tool_messages[-1].content)
            self.observation = json.loads(self.raw_observation)
            return AIMessage(content="langgraph observed RAG evidence")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "rag_search",
                    "args": self.arguments,
                    "id": "graph-rag-1",
                    "type": "tool_call",
                }
            ],
        )


def _shared_rag_registry() -> tuple[ToolRegistry, CountingRetriever]:
    documents = [
        TextDocument(
            root_id="knowledge",
            path="runtime.md",
            text="Agent Runtime 将工具结果作为 observation 交还给模型。",
        ),
        TextDocument(
            root_id="knowledge",
            path="checkpoint.md",
            text="LangGraph checkpoint 保存工作流执行状态。",
        ),
    ]
    chunks = [
        chunk
        for document in documents
        for chunk in chunk_document(document, max_chars=200)
    ]
    provider = DeterministicFakeEmbeddingProvider(dimensions=64)
    retriever = CountingRetriever(
        VectorRetriever(embed_chunks(chunks, provider), provider)
    )
    shared_tool = create_rag_search_tool(RAGSearchService(retriever))
    registry = ToolRegistry([shared_tool])
    assert registry.schemas == [shared_tool.schema]
    return registry, retriever


def test_same_rag_tool_produces_the_same_observation_in_both_runtimes() -> None:
    arguments = {"query": "Agent Runtime observation", "top_k": 2}
    registry, retriever = _shared_rag_registry()

    self_built_client = SelfBuiltRAGClient(arguments)
    self_built_result = AgentRuntime(
        client=self_built_client,  # type: ignore[arg-type]
        tools=registry,
        system_prompt="Use RAG evidence.",
    ).run("解释 Agent Runtime 的 observation")

    graph_model = LangGraphRAGModel(arguments)
    graph_result = build_leon_graph(graph_model, registry).invoke(
        {"messages": [HumanMessage(content="解释 Agent Runtime 的 observation")]}
    )

    assert self_built_result.answer == "self-built observed RAG evidence"
    assert graph_result["messages"][-1].content == "langgraph observed RAG evidence"
    assert self_built_client.raw_observation == graph_model.raw_observation
    assert self_built_client.observation == graph_model.observation
    assert self_built_client.observation is not None
    assert self_built_client.observation["count"] == 2
    assert retriever.calls == [
        ("Agent Runtime observation", 2),
        ("Agent Runtime observation", 2),
    ]
