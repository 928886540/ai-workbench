"""Optional provider-neutral reranking over retrieved candidates."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from rag_lab.retrieval import RetrievalHit


class RerankingError(RuntimeError):
    """Raised when a reranker violates the local result contract."""


class Reranker(Protocol):
    def rerank(
        self,
        query: str,
        hits: Sequence[RetrievalHit],
        *,
        top_n: int,
    ) -> list[RetrievalHit]: ...


class SiliconFlowReranker:
    """Adapt SiliconFlow's OpenAI-adjacent ``POST /v1/rerank`` endpoint."""

    def __init__(self, client: Any, *, model: str) -> None:
        cleaned_model = str(model or "").strip()
        if not cleaned_model:
            raise ValueError("reranker model is required")
        self._client = client
        self.model = cleaned_model

    def rerank(
        self,
        query: str,
        hits: Sequence[RetrievalHit],
        *,
        top_n: int,
    ) -> list[RetrievalHit]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
            raise ValueError("top_n must be a positive integer")
        items = list(hits)
        if not items:
            return []
        expected_count = min(top_n, len(items))
        try:
            response = self._client.post(
                "/rerank",
                json={
                    "model": self.model,
                    "query": query.strip(),
                    "documents": [hit.chunk.text for hit in items],
                    "top_n": expected_count,
                    "return_documents": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - stabilize provider errors
            raise RerankingError("reranking request failed") from exc

        if not isinstance(payload, Mapping):
            raise RerankingError("reranking response must be an object")
        raw_results = payload.get("results")
        if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes)):
            raise RerankingError("reranking response results are missing")
        if len(raw_results) != expected_count:
            raise RerankingError("reranking response returned the wrong result count")

        scored: list[tuple[int, float]] = []
        seen_indexes: set[int] = set()
        for result in raw_results:
            if not isinstance(result, Mapping):
                raise RerankingError("reranking result must be an object")
            index = result.get("index")
            score = result.get("relevance_score")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index < 0
                or index >= len(items)
                or index in seen_indexes
            ):
                raise RerankingError("reranking result indexes are invalid")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise RerankingError("reranking result score is invalid")
            numeric_score = float(score)
            if not math.isfinite(numeric_score):
                raise RerankingError("reranking result score is not finite")
            seen_indexes.add(index)
            scored.append((index, numeric_score))

        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return [
            RetrievalHit(
                chunk=items[index].chunk,
                score=score,
                rank=rank,
            )
            for rank, (index, score) in enumerate(scored, start=1)
        ]
