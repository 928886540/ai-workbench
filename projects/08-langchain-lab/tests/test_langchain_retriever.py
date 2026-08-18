from __future__ import annotations

import pytest
from langchain_lab.retriever import VectorRetrieverAdapter
from pydantic import ValidationError
from rag_lab import RetrievalHit, TextDocument, chunk_document


class StubRetriever:
    def __init__(self, hits: list[RetrievalHit]) -> None:
        self.hits = hits
        self.calls: list[tuple[str, int]] = []

    def retrieve(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        self.calls.append((query, top_k))
        return self.hits[:top_k]


def _hit() -> RetrievalHit:
    chunk = chunk_document(
        TextDocument(
            root_id="knowledge",
            path="runbook.md",
            text="Payment runbook evidence.",
        ),
        max_chars=200,
    )[0]
    return RetrievalHit(chunk=chunk, score=0.93, rank=1)


def test_retriever_maps_rag_hits_to_langchain_documents() -> None:
    backend = StubRetriever([_hit()])
    retriever = VectorRetrieverAdapter(backend=backend, top_k=2)

    documents = retriever.invoke("payment timeout")

    assert backend.calls == [("payment timeout", 2)]
    assert len(documents) == 1
    assert documents[0].page_content == "Payment runbook evidence."
    assert documents[0].metadata == {
        "citation": "knowledge:runbook.md:1-1",
        "chunk_id": "knowledge:runbook.md#chunk-0",
        "root_id": "knowledge",
        "path": "runbook.md",
        "start_line": 1,
        "end_line": 1,
        "score": 0.93,
        "rank": 1,
        "untrusted_content": True,
    }


def test_retriever_rejects_a_non_positive_top_k() -> None:
    with pytest.raises(ValidationError):
        VectorRetrieverAdapter(backend=StubRetriever([]), top_k=0)
