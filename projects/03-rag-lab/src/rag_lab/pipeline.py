"""Retrieve evidence, generate an answer, and score its citation contract."""

from __future__ import annotations

from dataclasses import dataclass

from rag_lab.citations import CitationContext, build_citation_context
from rag_lab.evaluation import citation_precision
from rag_lab.faithfulness import (
    FaithfulnessJudge,
    FaithfulnessReport,
    evaluate_faithfulness,
)
from rag_lab.generation import AnswerGenerator, extract_citations
from rag_lab.reranking import Reranker
from rag_lab.retrieval import RetrievalHit, VectorRetriever


@dataclass(frozen=True, slots=True)
class RAGAnswer:
    query: str
    text: str
    citations: tuple[str, ...]
    citation_precision: float
    hits: tuple[RetrievalHit, ...]
    context: CitationContext
    faithfulness: FaithfulnessReport | None

    def as_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "text": self.text,
            "citations": list(self.citations),
            "citation_precision": self.citation_precision,
            "hits": [
                {
                    "rank": hit.rank,
                    "score": hit.score,
                    "citation": hit.citation,
                }
                for hit in self.hits
            ],
            "faithfulness": (
                self.faithfulness.as_dict() if self.faithfulness is not None else None
            ),
        }


def answer_query(
    query: str,
    retriever: VectorRetriever,
    generator: AnswerGenerator,
    *,
    top_k: int = 3,
    context_max_chars: int = 4_000,
    faithfulness_judge: FaithfulnessJudge | None = None,
    reranker: Reranker | None = None,
    candidate_k: int | None = None,
) -> RAGAnswer:
    """Run one bounded RAG turn without granting retrieved text authority."""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    cleaned_query = query.strip()
    if reranker is None:
        hits = tuple(retriever.retrieve(cleaned_query, top_k=top_k))
    else:
        selected_candidate_k = candidate_k if candidate_k is not None else max(top_k * 2, 8)
        if (
            isinstance(selected_candidate_k, bool)
            or not isinstance(selected_candidate_k, int)
            or selected_candidate_k < top_k
        ):
            raise ValueError("candidate_k must be an integer greater than or equal to top_k")
        candidates = retriever.retrieve(cleaned_query, top_k=selected_candidate_k)
        hits = tuple(reranker.rerank(cleaned_query, candidates, top_n=top_k))
    context = build_citation_context(hits, max_chars=context_max_chars)
    if not hits:
        return RAGAnswer(
            query=cleaned_query,
            text="Insufficient retrieved evidence.",
            citations=(),
            citation_precision=0.0,
            hits=(),
            context=context,
            faithfulness=None,
        )

    answer = generator.generate(query=cleaned_query, context=context)
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("answer generator must return a non-empty string")
    citations = extract_citations(answer)
    faithfulness = (
        evaluate_faithfulness(answer, context, faithfulness_judge)
        if faithfulness_judge is not None
        else None
    )
    return RAGAnswer(
        query=cleaned_query,
        text=answer.strip(),
        citations=citations,
        citation_precision=citation_precision(
            citations,
            supported_citations=context.citations,
        ),
        hits=hits,
        context=context,
        faithfulness=faithfulness,
    )
