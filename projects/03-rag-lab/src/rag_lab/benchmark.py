"""Fixed-dataset retrieval benchmark for the RAG lab."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rag_lab.chunking import TextDocument, chunk_document
from rag_lab.embeddings import EmbeddingProvider, embed_chunks
from rag_lab.evaluation import score_retrieval
from rag_lab.reranking import Reranker
from rag_lab.retrieval import VectorRetriever

DEFAULT_DATASET_PATH = Path(__file__).with_name("data") / "retrieval-baseline.json"


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    case_id: str
    query: str
    relevant_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        if not self.query.strip():
            raise ValueError("query must be a non-empty string")
        if not self.relevant_paths or any(not path.strip() for path in self.relevant_paths):
            raise ValueError("relevant_paths must contain non-empty paths")
        if len(set(self.relevant_paths)) != len(self.relevant_paths):
            raise ValueError("relevant_paths cannot contain duplicates")


@dataclass(frozen=True, slots=True)
class RetrievalDataset:
    documents: tuple[TextDocument, ...]
    cases: tuple[RetrievalCase, ...]

    def __post_init__(self) -> None:
        if not self.documents:
            raise ValueError("dataset must contain documents")
        if not self.cases:
            raise ValueError("dataset must contain cases")
        paths = [document.path for document in self.documents]
        if len(set(paths)) != len(paths):
            raise ValueError("document paths must be unique")
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case ids must be unique")
        known_paths = set(paths)
        unknown_paths = {
            path
            for case in self.cases
            for path in case.relevant_paths
            if path not in known_paths
        }
        if unknown_paths:
            raise ValueError(f"cases reference unknown document paths: {sorted(unknown_paths)}")


@dataclass(frozen=True, slots=True)
class BenchmarkCaseResult:
    case_id: str
    query: str
    recall_at_k: float
    reciprocal_rank: float
    hit_citations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    top_k: int
    recall_at_k: float
    mean_reciprocal_rank: float
    cases: tuple[BenchmarkCaseResult, ...]

    @property
    def case_count(self) -> int:
        return len(self.cases)

    def as_dict(self) -> dict[str, object]:
        return {
            "case_count": self.case_count,
            "top_k": self.top_k,
            "recall_at_k": self.recall_at_k,
            "mean_reciprocal_rank": self.mean_reciprocal_rank,
            "cases": [
                {
                    "case_id": case.case_id,
                    "query": case.query,
                    "recall_at_k": case.recall_at_k,
                    "reciprocal_rank": case.reciprocal_rank,
                    "hit_citations": list(case.hit_citations),
                }
                for case in self.cases
            ],
        }


def _require_mapping(value: object, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _require_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _require_list(value: object, *, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def load_retrieval_dataset(path: str | Path = DEFAULT_DATASET_PATH) -> RetrievalDataset:
    """Load one UTF-8 benchmark dataset without accepting provider configuration."""

    dataset_path = Path(path)
    try:
        raw = json.loads(dataset_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to load retrieval dataset: {dataset_path}") from exc
    root = _require_mapping(raw, name="dataset")
    root_id = _require_string(root.get("root_id"), name="root_id")

    documents: list[TextDocument] = []
    for index, value in enumerate(_require_list(root.get("documents"), name="documents")):
        item = _require_mapping(value, name=f"documents[{index}]")
        documents.append(
            TextDocument(
                root_id=root_id,
                path=_require_string(item.get("path"), name=f"documents[{index}].path"),
                text=_require_string(item.get("text"), name=f"documents[{index}].text"),
            )
        )

    cases: list[RetrievalCase] = []
    for index, value in enumerate(_require_list(root.get("cases"), name="cases")):
        item = _require_mapping(value, name=f"cases[{index}]")
        relevant_values = _require_list(
            item.get("relevant_paths"), name=f"cases[{index}].relevant_paths"
        )
        cases.append(
            RetrievalCase(
                case_id=_require_string(item.get("id"), name=f"cases[{index}].id"),
                query=_require_string(item.get("query"), name=f"cases[{index}].query"),
                relevant_paths=tuple(
                    _require_string(path, name=f"cases[{index}].relevant_paths")
                    for path in relevant_values
                ),
            )
        )
    return RetrievalDataset(documents=tuple(documents), cases=tuple(cases))


def run_retrieval_benchmark(
    dataset: RetrievalDataset,
    provider: EmbeddingProvider,
    *,
    top_k: int = 3,
    max_chars: int = 800,
    batch_size: int = 32,
    reranker: Reranker | None = None,
    candidate_k: int | None = None,
) -> BenchmarkResult:
    """Index the fixed corpus once, then average Recall@K and RR across queries."""

    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    chunks = [
        chunk
        for document in dataset.documents
        for chunk in chunk_document(document, max_chars=max_chars)
    ]
    citations_by_path: dict[str, set[str]] = {}
    for chunk in chunks:
        citations_by_path.setdefault(chunk.path, set()).add(chunk.citation)

    retriever = VectorRetriever(
        embed_chunks(chunks, provider, batch_size=batch_size),
        provider,
    )
    results: list[BenchmarkCaseResult] = []
    for case in dataset.cases:
        if reranker is None:
            hits = retriever.retrieve(case.query, top_k=top_k)
        else:
            selected_candidate_k = candidate_k if candidate_k is not None else max(top_k * 2, 8)
            if (
                isinstance(selected_candidate_k, bool)
                or not isinstance(selected_candidate_k, int)
                or selected_candidate_k < top_k
            ):
                raise ValueError(
                    "candidate_k must be an integer greater than or equal to top_k"
                )
            candidates = retriever.retrieve(case.query, top_k=selected_candidate_k)
            hits = reranker.rerank(case.query, candidates, top_n=top_k)
        relevant_citations = {
            citation
            for path in case.relevant_paths
            for citation in citations_by_path.get(path, set())
        }
        score = score_retrieval(
            hits,
            relevant_citations=relevant_citations,
            k=top_k,
        )
        results.append(
            BenchmarkCaseResult(
                case_id=case.case_id,
                query=case.query,
                recall_at_k=score.recall_at_k,
                reciprocal_rank=score.reciprocal_rank,
                hit_citations=tuple(hit.citation for hit in hits),
            )
        )

    case_count = len(results)
    return BenchmarkResult(
        top_k=top_k,
        recall_at_k=sum(case.recall_at_k for case in results) / case_count,
        mean_reciprocal_rank=(
            sum(case.reciprocal_rank for case in results) / case_count
        ),
        cases=tuple(results),
    )
