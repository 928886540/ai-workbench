from __future__ import annotations

import pytest
from rag_lab.chunking import TextDocument, chunk_document
from rag_lab.evaluation import citation_precision, score_retrieval
from rag_lab.retrieval import RetrievalHit


def _hits() -> list[RetrievalHit]:
    chunks = chunk_document(
        TextDocument(root_id="docs", path="guide.md", text="alpha\nbeta\ngamma"),
        max_chars=6,
    )
    return [
        RetrievalHit(chunk=chunk, score=1.0 - index / 10, rank=index + 1)
        for index, chunk in enumerate(chunks)
    ]


def test_retrieval_score_reports_recall_at_k_and_reciprocal_rank() -> None:
    hits = _hits()

    score = score_retrieval(
        hits,
        relevant_citations={hits[1].citation, "docs:missing.md:1-1"},
        k=2,
    )

    assert score.recall_at_k == pytest.approx(0.5)
    assert score.reciprocal_rank == pytest.approx(0.5)


def test_retrieval_score_does_not_double_count_duplicate_citations() -> None:
    hit = _hits()[0]

    score = score_retrieval(
        [hit, RetrievalHit(chunk=hit.chunk, score=0.9, rank=2)],
        relevant_citations={hit.citation},
        k=2,
    )

    assert score.recall_at_k == pytest.approx(1.0)
    assert score.reciprocal_rank == pytest.approx(1.0)


def test_retrieval_score_returns_zero_without_relevant_hit() -> None:
    score = score_retrieval(
        _hits(),
        relevant_citations={"docs:missing.md:1-1"},
        k=3,
    )

    assert score.recall_at_k == 0.0
    assert score.reciprocal_rank == 0.0


def test_citation_precision_penalizes_unsupported_sources() -> None:
    precision = citation_precision(
        ["docs:guide.md:1-1", "docs:invented.md:1-1"],
        supported_citations={"docs:guide.md:1-1", "docs:guide.md:2-2"},
    )

    assert precision == pytest.approx(0.5)


def test_citation_precision_ignores_duplicate_answer_citations() -> None:
    precision = citation_precision(
        ["docs:guide.md:1-1", "docs:guide.md:1-1"],
        supported_citations={"docs:guide.md:1-1"},
    )

    assert precision == pytest.approx(1.0)


def test_citation_precision_is_zero_when_answer_has_no_citations() -> None:
    assert citation_precision([], supported_citations={"docs:guide.md:1-1"}) == 0.0


def test_retrieval_score_requires_relevant_citations() -> None:
    with pytest.raises(ValueError, match="relevant_citations"):
        score_retrieval(_hits(), relevant_citations=set(), k=2)


@pytest.mark.parametrize("k", [True, 0, -1])
def test_retrieval_score_rejects_invalid_k(k: int) -> None:
    with pytest.raises(ValueError, match="k"):
        score_retrieval(
            _hits(),
            relevant_citations={"docs:guide.md:1-1"},
            k=k,
        )
