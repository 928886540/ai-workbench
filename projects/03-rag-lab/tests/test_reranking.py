from __future__ import annotations

import math
from types import SimpleNamespace

import pytest
from rag_lab.chunking import TextDocument, chunk_document
from rag_lab.reranking import RerankingError, SiliconFlowReranker
from rag_lab.retrieval import RetrievalHit


class FakeResponse:
    def __init__(self, payload, error: Exception | None = None) -> None:  # noqa: ANN001
        self.payload = payload
        self.error = error

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error

    def json(self):  # noqa: ANN201
        return self.payload


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, path: str, *, json: dict[str, object]):  # noqa: A002
        self.calls.append((path, json))
        return self.response


def _hits() -> list[RetrievalHit]:
    return [
        RetrievalHit(
            chunk=chunk_document(
                TextDocument(root_id="docs", path=f"doc-{index}.md", text=text)
            )[0],
            score=1.0 - index / 10,
            rank=index + 1,
        )
        for index, text in enumerate(("first", "second", "third"))
    ]


def test_reranker_posts_candidates_and_rebuilds_rank() -> None:
    client = FakeClient(
        FakeResponse(
            {
                "results": [
                    {"index": 2, "relevance_score": 0.7},
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.8},
                ]
            }
        )
    )
    hits = _hits()
    original = list(hits)

    reranked = SiliconFlowReranker(client, model="BAAI/bge-reranker-v2-m3").rerank(
        "  which document?  ", hits, top_n=3
    )

    assert client.calls == [
        (
            "/rerank",
            {
                "model": "BAAI/bge-reranker-v2-m3",
                "query": "which document?",
                "documents": ["first", "second", "third"],
                "top_n": 3,
                "return_documents": False,
            },
        )
    ]
    assert [hit.chunk.text for hit in reranked] == ["first", "second", "third"]
    assert [hit.score for hit in reranked] == pytest.approx([0.9, 0.8, 0.7])
    assert [hit.rank for hit in reranked] == [1, 2, 3]
    assert hits == original


def test_reranker_limits_requested_top_n_to_candidates() -> None:
    client = FakeClient(
        FakeResponse(
            {
                "results": [
                    {"index": 2, "relevance_score": 0.7},
                    {"index": 1, "relevance_score": 0.8},
                    {"index": 0, "relevance_score": 0.9},
                ]
            }
        )
    )

    result = SiliconFlowReranker(client, model="reranker").rerank(
        "query", _hits(), top_n=8
    )

    assert len(result) == 3
    assert client.calls[0][1]["top_n"] == 3


def test_reranker_returns_empty_without_network_call() -> None:
    client = FakeClient(FakeResponse({"results": []}))

    assert SiliconFlowReranker(client, model="reranker").rerank("query", [], top_n=1) == []
    assert client.calls == []


@pytest.mark.parametrize("query", ["", "   "])
def test_reranker_rejects_blank_query(query: str) -> None:
    with pytest.raises(ValueError, match="query"):
        SiliconFlowReranker(FakeClient(FakeResponse({})), model="reranker").rerank(
            query, _hits(), top_n=1
        )


@pytest.mark.parametrize("top_n", [True, 0, -1])
def test_reranker_rejects_invalid_top_n(top_n: int) -> None:
    with pytest.raises(ValueError, match="top_n"):
        SiliconFlowReranker(FakeClient(FakeResponse({})), model="reranker").rerank(
            "query", _hits(), top_n=top_n
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": "invalid"},
        {"results": []},
        {"results": [{"index": 9, "relevance_score": 0.5}]},
        {"results": [{"index": 0, "relevance_score": math.nan}]},
        {"results": [{"index": 0, "relevance_score": math.inf}]},
    ],
)
def test_reranker_rejects_invalid_response_contract(payload) -> None:  # noqa: ANN001
    with pytest.raises(RerankingError):
        SiliconFlowReranker(FakeClient(FakeResponse(payload)), model="reranker").rerank(
            "query", _hits(), top_n=1
        )


def test_reranker_rejects_duplicate_indexes() -> None:
    payload = {
        "results": [
            {"index": 0, "relevance_score": 0.5},
            {"index": 0, "relevance_score": 0.4},
        ]
    }

    with pytest.raises(RerankingError, match="indexes"):
        SiliconFlowReranker(
            FakeClient(FakeResponse(payload)), model="reranker"
        ).rerank("query", _hits(), top_n=2)


def test_reranker_wraps_transport_failure() -> None:
    client = FakeClient(FakeResponse({}, error=RuntimeError("network")))

    with pytest.raises(RerankingError, match="request failed"):
        SiliconFlowReranker(client, model="reranker").rerank("query", _hits(), top_n=1)


def test_reranker_requires_model() -> None:
    with pytest.raises(ValueError, match="model"):
        SiliconFlowReranker(SimpleNamespace(), model=" ")
