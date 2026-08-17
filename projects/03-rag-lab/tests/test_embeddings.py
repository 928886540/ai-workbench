from __future__ import annotations

from types import SimpleNamespace

import pytest
from rag_lab.chunking import TextDocument, chunk_document
from rag_lab.embeddings import (
    EmbeddingError,
    OpenAIEmbeddingProvider,
    embed_chunks,
)


class FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts):  # noqa: ANN001, ANN201
        self.calls.append(list(texts))
        return [[float(len(text)), 1.0] for text in texts]


def _chunks():  # noqa: ANN202
    return chunk_document(
        TextDocument(root_id="docs", path="guide.md", text="alpha\nbeta\ngamma"),
        max_chars=10,
    )


def test_embed_chunks_batches_and_preserves_chunk_identity() -> None:
    provider = FakeEmbeddingProvider()

    embedded = embed_chunks(_chunks(), provider, batch_size=1)

    assert provider.calls == [["alpha"], ["beta\ngamma"]]
    assert [item.chunk.citation for item in embedded] == [
        "docs:guide.md:1-1",
        "docs:guide.md:2-3",
    ]
    assert all(
        sum(value * value for value in item.vector) == pytest.approx(1.0)
        for item in embedded
    )


def test_embed_chunks_does_not_call_provider_for_empty_input() -> None:
    provider = FakeEmbeddingProvider()

    assert embed_chunks([], provider) == []
    assert provider.calls == []


@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [[1.0], [2.0]],
        [[]],
        [[0.0, 0.0]],
        [[float("nan"), 1.0]],
    ],
)
def test_embed_chunks_rejects_invalid_provider_output(vectors) -> None:  # noqa: ANN001
    class InvalidProvider:
        def embed(self, texts):  # noqa: ANN001, ANN201, ARG002
            return vectors

    with pytest.raises(EmbeddingError):
        embed_chunks(_chunks()[:1], InvalidProvider())


def test_embed_chunks_rejects_dimension_changes_between_batches() -> None:
    class ChangingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def embed(self, texts):  # noqa: ANN001, ANN201, ARG002
            self.calls += 1
            return [[1.0, 2.0]] if self.calls == 1 else [[1.0, 2.0, 3.0]]

    with pytest.raises(EmbeddingError, match="dimension"):
        embed_chunks(_chunks(), ChangingProvider(), batch_size=1)


def test_openai_embedding_provider_restores_response_index_order() -> None:
    class FakeEmbeddings:
        def create(self, **kwargs):  # noqa: ANN003, ANN201
            assert kwargs == {"model": "text-embedding-test", "input": ["a", "b"]}
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=1, embedding=[0.0, 1.0]),
                    SimpleNamespace(index=0, embedding=[1.0, 0.0]),
                ]
            )

    client = SimpleNamespace(embeddings=FakeEmbeddings())
    provider = OpenAIEmbeddingProvider(client, model="text-embedding-test")

    assert provider.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]


def test_openai_embedding_provider_rejects_missing_indexes() -> None:
    class FakeEmbeddings:
        def create(self, **kwargs):  # noqa: ANN003, ANN201, ARG002
            return SimpleNamespace(
                data=[SimpleNamespace(index=1, embedding=[0.0, 1.0])]
            )

    provider = OpenAIEmbeddingProvider(
        SimpleNamespace(embeddings=FakeEmbeddings()),
        model="text-embedding-test",
    )

    with pytest.raises(EmbeddingError, match="indexes"):
        provider.embed(["a", "b"])
