"""Deterministic retrieval and citation metrics."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from rag_lab.retrieval import RetrievalHit


@dataclass(frozen=True, slots=True)
class RetrievalScore:
    recall_at_k: float
    reciprocal_rank: float


def _citation_set(values: Iterable[str], *, name: str) -> set[str]:
    citations: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must contain non-empty citation strings")
        citations.add(value.strip())
    return citations


def score_retrieval(
    hits: Sequence[RetrievalHit],
    *,
    relevant_citations: Iterable[str],
    k: int,
) -> RetrievalScore:
    """Score one query; average reciprocal_rank across cases to obtain MRR."""

    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive integer")
    relevant = _citation_set(relevant_citations, name="relevant_citations")
    if not relevant:
        raise ValueError("relevant_citations cannot be empty")

    retrieved = list(hits)[:k]
    found = {hit.citation for hit in retrieved if hit.citation in relevant}
    first_relevant_rank = next(
        (
            rank
            for rank, hit in enumerate(retrieved, start=1)
            if hit.citation in relevant
        ),
        None,
    )
    return RetrievalScore(
        recall_at_k=len(found) / len(relevant),
        reciprocal_rank=(1.0 / first_relevant_rank if first_relevant_rank else 0.0),
    )


def citation_precision(
    cited_citations: Iterable[str],
    *,
    supported_citations: Iterable[str],
) -> float:
    """Measure which unique answer citations came from the retrieved evidence."""

    cited = _citation_set(cited_citations, name="cited_citations")
    supported = _citation_set(supported_citations, name="supported_citations")
    if not cited:
        return 0.0
    return len(cited & supported) / len(cited)
