"""Small in-memory cosine retriever for the first RAG loop."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rag_lab.chunking import Chunk
from rag_lab.embeddings import (
    EmbeddedChunk,
    EmbeddingProvider,
    embed_query,
    normalize_embedding,
)


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    chunk: Chunk
    score: float
    rank: int

    @property
    def citation(self) -> str:
        return self.chunk.citation


class VectorRetriever:
    """Search one immutable in-memory chunk snapshot with cosine similarity."""

    def __init__(
        self,
        items: Sequence[EmbeddedChunk],
        provider: EmbeddingProvider,
    ) -> None:
        self._provider = provider
        self._items: list[EmbeddedChunk] = []
        self._dimension: int | None = None
        chunk_ids: set[str] = set()
        for item in items:
            if item.chunk.chunk_id in chunk_ids:
                raise ValueError(f"duplicate chunk_id: {item.chunk.chunk_id}")
            vector = normalize_embedding(
                item.vector,
                expected_dimension=self._dimension,
            )
            if self._dimension is None:
                self._dimension = len(vector)
            chunk_ids.add(item.chunk.chunk_id)
            self._items.append(EmbeddedChunk(chunk=item.chunk, vector=vector))

    def retrieve(self, query: str, *, top_k: int = 5) -> list[RetrievalHit]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise ValueError("top_k must be a positive integer")
        if not self._items or self._dimension is None:
            return []

        query_vector = embed_query(
            query,
            self._provider,
            expected_dimension=self._dimension,
        )
        scored = [
            (
                item,
                sum(
                    left * right
                    for left, right in zip(item.vector, query_vector, strict=True)
                ),
            )
            for item in self._items
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [
            RetrievalHit(chunk=item.chunk, score=score, rank=index)
            for index, (item, score) in enumerate(scored[:top_k], start=1)
        ]
