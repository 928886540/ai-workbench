"""Provider-free tests for the shared RAG AgentTool boundary."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from rag_lab.chunking import TextDocument, chunk_document
from rag_lab.embeddings import embed_chunks
from rag_lab.providers import DeterministicFakeEmbeddingProvider
from rag_lab.retrieval import RetrievalHit, VectorRetriever
from rag_lab.tools import (
    MAX_RAG_RESULT_CHARS,
    RAGSearchService,
    create_rag_search_tool,
)
from workbench_core.agent import ToolRegistry


class StubRetriever:
    def __init__(self, hits: list[RetrievalHit]) -> None:
        self.hits = hits
        self.calls: list[tuple[str, int]] = []

    def retrieve(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        self.calls.append((query, top_k))
        return self.hits[:top_k]


def _hit(*, text: str = "RAG 使用 citation 标记证据来源。") -> RetrievalHit:
    chunk = chunk_document(
        TextDocument(root_id="knowledge", path="rag.md", text=text),
        max_chars=800,
    )[0]
    return RetrievalHit(chunk=chunk, score=0.91, rank=1)


def test_rag_search_tool_returns_bounded_untrusted_evidence() -> None:
    retriever = StubRetriever([_hit()])
    registry = ToolRegistry([create_rag_search_tool(RAGSearchService(retriever))])

    result = registry.execute(
        "rag_search",
        {"query": "  RAG 怎么引用证据？  ", "top_k": 3},
    )

    assert retriever.calls == [("RAG 怎么引用证据？", 3)]
    assert result == {
        "ok": True,
        "untrusted_content": True,
        "count": 1,
        "hits": [
            {
                "rank": 1,
                "score": 0.91,
                "citation": "knowledge:rag.md:1-1",
                "text": "RAG 使用 citation 标记证据来源。",
                "untrusted_content": True,
                "truncated": False,
            }
        ],
    }

    schema = registry.schemas[0]["function"]
    assert schema["name"] == "rag_search"
    assert schema["parameters"]["properties"]["top_k"]["maximum"] == 5


def test_rag_search_audit_removes_query_and_evidence_text() -> None:
    registry = ToolRegistry(
        [create_rag_search_tool(RAGSearchService(StubRetriever([_hit()])))],
    )
    arguments = {"query": "private query", "top_k": 2}
    result = registry.execute("rag_search", arguments)

    assert registry.audit_arguments("rag_search", arguments) == {"top_k": 2}
    audit = registry.audit_result("rag_search", result)
    assert audit == {
        "ok": True,
        "untrusted_content": True,
        "count": 1,
        "citations": ["knowledge:rag.md:1-1"],
    }
    assert "private query" not in str(audit)
    assert result["hits"][0]["text"] not in str(audit)


def test_rag_search_validates_at_the_service_boundary() -> None:
    retriever = StubRetriever([_hit()])
    service = RAGSearchService(retriever)

    assert service.search(query="", top_k=3)["error_code"] == "invalid_query"
    assert service.search(query="q", top_k=0)["error_code"] == "invalid_top_k"
    assert service.search(query="q", top_k=6)["error_code"] == "invalid_top_k"
    assert retriever.calls == []


def test_rag_search_converts_retriever_failures_to_stable_errors() -> None:
    class BrokenRetriever:
        def retrieve(self, query: str, *, top_k: int) -> list[Any]:
            raise RuntimeError(f"private provider failure: {query}/{top_k}")

    result = RAGSearchService(BrokenRetriever()).search(query="query", top_k=2)

    assert result == {
        "ok": False,
        "error_code": "retrieval_failed",
        "error": "RAG retrieval failed.",
    }


def test_rag_search_returns_an_empty_bounded_result() -> None:
    result = RAGSearchService(StubRetriever([])).search(query="missing", top_k=5)

    assert result == {
        "ok": True,
        "untrusted_content": True,
        "count": 0,
        "hits": [],
    }


def test_rag_search_rejects_unsafe_citations_in_result_and_audit() -> None:
    unsafe_hit = _hit()
    unsafe_hit = replace(
        unsafe_hit,
        chunk=replace(unsafe_hit.chunk, citation="knowledge:../secret.md:1-1"),
    )
    registry = ToolRegistry(
        [create_rag_search_tool(RAGSearchService(StubRetriever([unsafe_hit])))],
    )

    result = registry.execute("rag_search", {"query": "query"})

    assert result["error_code"] == "retrieval_failed"
    assert registry.audit_result(
        "rag_search",
        {
            "ok": True,
            "hits": [{"citation": "knowledge:../secret.md:1-1"}],
        },
    ) == {"audit_error": "invalid_result"}

    bracket_hit = _hit()
    bracket_hit = replace(
        bracket_hit,
        chunk=replace(
            bracket_hit.chunk,
            path="rag[final].md",
            citation="knowledge:rag[final].md:1-1",
        ),
    )
    assert RAGSearchService(StubRetriever([bracket_hit])).search(
        query="query"
    )["error_code"] == "retrieval_failed"


def test_rag_search_only_reads_the_requested_hit_slice() -> None:
    class SliceOnlyHits(Sequence[RetrievalHit]):
        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int | slice) -> RetrievalHit | list[RetrievalHit]:
            if not isinstance(index, slice):
                raise AssertionError("the service must not materialize the full sequence")
            return [_hit()][index]

    class SliceOnlyRetriever:
        def retrieve(self, query: str, *, top_k: int) -> Sequence[RetrievalHit]:
            return SliceOnlyHits()

    result = RAGSearchService(SliceOnlyRetriever()).search(query="query", top_k=1)

    assert result["ok"] is True
    assert result["count"] == 1


def test_rag_search_result_stays_below_runtime_observation_limit() -> None:
    hits: list[RetrievalHit] = []
    for index in range(1, 6):
        prefix = f"evidence-{index}-"
        hit = _hit()
        hits.append(
            replace(
                hit,
                chunk=replace(
                    hit.chunk,
                    text=prefix + "x" * (1000 - len(prefix)),
                ),
                rank=index,
            )
        )

    result = RAGSearchService(StubRetriever(hits)).search(query="query", top_k=5)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["ok"] is True
    assert len(serialized) <= MAX_RAG_RESULT_CHARS < 6000
    assert result["hits"][-1]["truncated"] is True


def test_rag_search_executes_the_real_offline_rag_pipeline() -> None:
    documents = [
        TextDocument(
            root_id="knowledge",
            path="rag.md",
            text="RAG citation 给检索证据标记来源。",
        ),
        TextDocument(
            root_id="knowledge",
            path="graph.md",
            text="LangGraph checkpoint 保存工作流状态。",
        ),
    ]
    chunks = [
        chunk
        for document in documents
        for chunk in chunk_document(document, max_chars=200)
    ]
    provider = DeterministicFakeEmbeddingProvider(dimensions=64)
    retriever = VectorRetriever(embed_chunks(chunks, provider), provider)

    result = RAGSearchService(retriever).search(
        query="RAG citation 检索证据来源",
        top_k=1,
    )

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["hits"][0]["citation"] == "knowledge:rag.md:1-1"
    assert "citation" in result["hits"][0]["text"]
