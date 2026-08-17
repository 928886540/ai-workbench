"""Batch RAG evaluation across retrieval, citations, and faithfulness."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rag_lab.benchmark import RetrievalCase, RetrievalDataset
from rag_lab.chunking import chunk_document
from rag_lab.embeddings import EmbeddingProvider, embed_chunks
from rag_lab.evaluation import score_retrieval
from rag_lab.faithfulness import FaithfulnessJudge
from rag_lab.generation import AnswerGenerator
from rag_lab.pipeline import RAGAnswer, answer_query
from rag_lab.reranking import Reranker
from rag_lab.retrieval import VectorRetriever


@dataclass(frozen=True, slots=True)
class RAGSuiteCaseResult:
    case_id: str
    query: str
    recall_at_k: float
    reciprocal_rank: float
    answer: RAGAnswer

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "query": self.query,
            "recall_at_k": self.recall_at_k,
            "reciprocal_rank": self.reciprocal_rank,
            "answer": self.answer.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RAGSuiteResult:
    top_k: int
    cases: tuple[RAGSuiteCaseResult, ...]

    @property
    def case_count(self) -> int:
        return len(self.cases)

    @property
    def recall_at_k(self) -> float:
        return sum(case.recall_at_k for case in self.cases) / self.case_count

    @property
    def mean_reciprocal_rank(self) -> float:
        return sum(case.reciprocal_rank for case in self.cases) / self.case_count

    @property
    def citation_rate(self) -> float:
        return sum(bool(case.answer.citations) for case in self.cases) / self.case_count

    @property
    def mean_citation_precision(self) -> float:
        return (
            sum(case.answer.citation_precision for case in self.cases) / self.case_count
        )

    @property
    def faithfulness_scores(self) -> tuple[float, ...]:
        return tuple(
            score
            for case in self.cases
            if case.answer.faithfulness is not None
            for score in [case.answer.faithfulness.score]
            if score is not None
        )

    @property
    def mean_faithfulness(self) -> float | None:
        scores = self.faithfulness_scores
        if not scores:
            return None
        return sum(scores) / len(scores)

    def as_dict(self) -> dict[str, object]:
        return {
            "case_count": self.case_count,
            "top_k": self.top_k,
            "recall_at_k": self.recall_at_k,
            "mean_reciprocal_rank": self.mean_reciprocal_rank,
            "citation_rate": self.citation_rate,
            "mean_citation_precision": self.mean_citation_precision,
            "judged_case_count": len(self.faithfulness_scores),
            "mean_faithfulness": self.mean_faithfulness,
            "cases": [case.as_dict() for case in self.cases],
        }


def _select_cases(
    dataset: RetrievalDataset,
    case_ids: Sequence[str] | None,
) -> tuple[RetrievalCase, ...]:
    if case_ids is None:
        return dataset.cases
    requested = [str(case_id).strip() for case_id in case_ids]
    if not requested or any(not case_id for case_id in requested):
        raise ValueError("case_ids must contain non-empty ids")
    if len(set(requested)) != len(requested):
        raise ValueError("case_ids cannot contain duplicates")
    by_id = {case.case_id: case for case in dataset.cases}
    unknown = [case_id for case_id in requested if case_id not in by_id]
    if unknown:
        raise ValueError(f"unknown case ids: {unknown}")
    return tuple(by_id[case_id] for case_id in requested)


def run_rag_suite(
    dataset: RetrievalDataset,
    embedding_provider: EmbeddingProvider,
    answer_generator: AnswerGenerator,
    *,
    faithfulness_judge: FaithfulnessJudge | None = None,
    case_ids: Sequence[str] | None = None,
    top_k: int = 3,
    max_chars: int = 800,
    batch_size: int = 32,
    context_max_chars: int = 4_000,
    reranker: Reranker | None = None,
    candidate_k: int | None = None,
) -> RAGSuiteResult:
    """Build one index, then evaluate selected RAG cases end to end."""

    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    selected_cases = _select_cases(dataset, case_ids)
    chunks = [
        chunk
        for document in dataset.documents
        for chunk in chunk_document(document, max_chars=max_chars)
    ]
    citations_by_path: dict[str, set[str]] = {}
    for chunk in chunks:
        citations_by_path.setdefault(chunk.path, set()).add(chunk.citation)
    retriever = VectorRetriever(
        embed_chunks(chunks, embedding_provider, batch_size=batch_size),
        embedding_provider,
    )

    results: list[RAGSuiteCaseResult] = []
    for case in selected_cases:
        answer = answer_query(
            case.query,
            retriever,
            answer_generator,
            top_k=top_k,
            context_max_chars=context_max_chars,
            faithfulness_judge=faithfulness_judge,
            reranker=reranker,
            candidate_k=candidate_k,
        )
        relevant_citations = {
            citation
            for path in case.relevant_paths
            for citation in citations_by_path.get(path, set())
        }
        retrieval_score = score_retrieval(
            answer.hits,
            relevant_citations=relevant_citations,
            k=top_k,
        )
        results.append(
            RAGSuiteCaseResult(
                case_id=case.case_id,
                query=case.query,
                recall_at_k=retrieval_score.recall_at_k,
                reciprocal_rank=retrieval_score.reciprocal_rank,
                answer=answer,
            )
        )
    return RAGSuiteResult(top_k=top_k, cases=tuple(results))
