from __future__ import annotations

import pytest
from rag_lab.chunking import TextDocument, chunk_document
from rag_lab.embeddings import EmbeddedChunk, EmbeddingError
from rag_lab.retrieval import VectorRetriever


class QueryProvider:
    def __init__(self, vector) -> None:  # noqa: ANN001
        self.vector = vector
        self.calls: list[list[str]] = []

    def embed(self, texts):  # noqa: ANN001, ANN201
        self.calls.append(list(texts))
        return [self.vector for _ in texts]


def _embedded_chunks() -> list[EmbeddedChunk]:
    chunks = chunk_document(
        TextDocument(root_id="docs", path="guide.md", text="alpha\nbeta\ngamma"),
        max_chars=10,
    )
    return [
        EmbeddedChunk(chunk=chunks[0], vector=(1.0, 0.0)),
        EmbeddedChunk(chunk=chunks[1], vector=(0.8, 0.6)),
    ]


def test_retriever_returns_ranked_hits_with_citations() -> None:
    provider = QueryProvider([1.0, 0.0])
    retriever = VectorRetriever(_embedded_chunks(), provider)

    hits = retriever.retrieve("alpha", top_k=2)

    assert provider.calls == [["alpha"]]
    assert [hit.rank for hit in hits] == [1, 2]
    assert [hit.citation for hit in hits] == [
        "docs:guide.md:1-1",
        "docs:guide.md:2-3",
    ]
    assert [hit.score for hit in hits] == pytest.approx([1.0, 0.8])


def test_retriever_keeps_index_order_for_equal_scores() -> None:
    items = _embedded_chunks()
    tied = [
        EmbeddedChunk(chunk=items[1].chunk, vector=(1.0, 0.0)),
        EmbeddedChunk(chunk=items[0].chunk, vector=(1.0, 0.0)),
    ]
    retriever = VectorRetriever(tied, QueryProvider([1.0, 0.0]))

    hits = retriever.retrieve("alpha", top_k=2)

    assert [hit.chunk.chunk_id for hit in hits] == [
        tied[0].chunk.chunk_id,
        tied[1].chunk.chunk_id,
    ]


def test_retriever_limits_top_k_to_available_chunks() -> None:
    retriever = VectorRetriever(_embedded_chunks(), QueryProvider([0.0, 1.0]))

    hits = retriever.retrieve("gamma", top_k=10)

    assert len(hits) == 2
    assert hits[0].citation == "docs:guide.md:2-3"


def test_retriever_does_not_embed_query_for_empty_index() -> None:
    provider = QueryProvider([1.0, 0.0])
    retriever = VectorRetriever([], provider)

    assert retriever.retrieve("alpha") == []
    assert provider.calls == []


@pytest.mark.parametrize("query", ["", "   "])
def test_retriever_rejects_blank_query(query: str) -> None:
    retriever = VectorRetriever(_embedded_chunks(), QueryProvider([1.0, 0.0]))

    with pytest.raises(ValueError, match="query"):
        retriever.retrieve(query)


@pytest.mark.parametrize("top_k", [True, 0, -1])
def test_retriever_rejects_invalid_top_k(top_k: int) -> None:
    retriever = VectorRetriever(_embedded_chunks(), QueryProvider([1.0, 0.0]))

    with pytest.raises(ValueError, match="top_k"):
        retriever.retrieve("alpha", top_k=top_k)


def test_retriever_rejects_query_dimension_mismatch() -> None:
    retriever = VectorRetriever(_embedded_chunks(), QueryProvider([1.0, 0.0, 0.0]))

    with pytest.raises(EmbeddingError, match="dimension"):
        retriever.retrieve("alpha")


def test_retriever_rejects_duplicate_chunk_ids() -> None:
    item = _embedded_chunks()[0]

    with pytest.raises(ValueError, match="duplicate"):
        VectorRetriever([item, item], QueryProvider([1.0, 0.0]))
