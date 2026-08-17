"""Provider-neutral embedding contracts for RAG chunks."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from rag_lab.chunking import Chunk


class EmbeddingError(RuntimeError):
    """Raised when an embedding provider violates the local contract."""


class EmbeddingProvider(Protocol):
    """Small provider boundary used by indexing and query embedding."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class EmbeddedChunk:
    chunk: Chunk
    vector: tuple[float, ...]


class OpenAIEmbeddingProvider:
    """Adapt an injected OpenAI-compatible client without owning its credentials."""

    def __init__(self, client: Any, *, model: str) -> None:
        cleaned_model = str(model or "").strip()
        if not cleaned_model:
            raise ValueError("embedding model is required")
        self._client = client
        self.model = cleaned_model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        values = list(texts)
        if not values:
            return []
        try:
            response = self._client.embeddings.create(model=self.model, input=values)
        except Exception as exc:  # noqa: BLE001 - provider errors become a stable boundary
            raise EmbeddingError("embedding request failed") from exc

        data = getattr(response, "data", None)
        if not isinstance(data, Sequence):
            raise EmbeddingError("embedding response data is missing")
        indexed: dict[int, list[float]] = {}
        for item in data:
            index = getattr(item, "index", None)
            vector = getattr(item, "embedding", None)
            if not isinstance(index, int) or index in indexed or not isinstance(vector, Sequence):
                raise EmbeddingError("embedding response indexes are invalid")
            indexed[index] = list(vector)
        if set(indexed) != set(range(len(values))):
            raise EmbeddingError("embedding response indexes are incomplete")
        return [indexed[index] for index in range(len(values))]


def normalize_embedding(
    raw_vector: object,
    *,
    expected_dimension: int | None,
) -> tuple[float, ...]:
    if not isinstance(raw_vector, Sequence) or isinstance(raw_vector, (str, bytes)):
        raise EmbeddingError("embedding vector must be a numeric sequence")
    vector: list[float] = []
    for value in raw_vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingError("embedding vector contains a non-numeric value")
        number = float(value)
        if not math.isfinite(number):
            raise EmbeddingError("embedding vector contains a non-finite value")
        vector.append(number)
    if not vector:
        raise EmbeddingError("embedding vector cannot be empty")
    if expected_dimension is not None and len(vector) != expected_dimension:
        raise EmbeddingError("embedding vector dimension changed between batches")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise EmbeddingError("embedding vector cannot have zero magnitude")
    return tuple(value / norm for value in vector)


def embed_chunks(
    chunks: Sequence[Chunk],
    provider: EmbeddingProvider,
    *,
    batch_size: int = 32,
) -> list[EmbeddedChunk]:
    """Embed chunks in bounded batches and normalize vectors for cosine retrieval."""

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    items = list(chunks)
    if not items:
        return []

    embedded: list[EmbeddedChunk] = []
    dimension: int | None = None
    for offset in range(0, len(items), batch_size):
        batch = items[offset : offset + batch_size]
        try:
            raw_vectors = provider.embed([chunk.text for chunk in batch])
        except EmbeddingError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate arbitrary provider adapters
            raise EmbeddingError("embedding provider failed") from exc
        if not isinstance(raw_vectors, Sequence) or len(raw_vectors) != len(batch):
            raise EmbeddingError("embedding provider returned the wrong vector count")
        for chunk, raw_vector in zip(batch, raw_vectors, strict=True):
            vector = normalize_embedding(raw_vector, expected_dimension=dimension)
            if dimension is None:
                dimension = len(vector)
            embedded.append(EmbeddedChunk(chunk=chunk, vector=vector))
    return embedded


def embed_query(
    query: str,
    provider: EmbeddingProvider,
    *,
    expected_dimension: int,
) -> tuple[float, ...]:
    """Embed one query using the same validation and normalization as chunks."""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    try:
        raw_vectors = provider.embed([query])
    except EmbeddingError:
        raise
    except Exception as exc:  # noqa: BLE001 - isolate arbitrary provider adapters
        raise EmbeddingError("embedding provider failed") from exc
    if not isinstance(raw_vectors, Sequence) or len(raw_vectors) != 1:
        raise EmbeddingError("embedding provider returned the wrong vector count")
    return normalize_embedding(
        raw_vectors[0],
        expected_dimension=expected_dimension,
    )
