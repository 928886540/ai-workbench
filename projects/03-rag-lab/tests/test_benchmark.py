from __future__ import annotations

import json

import pytest
from rag_lab.benchmark import (
    DEFAULT_DATASET_PATH,
    RetrievalCase,
    RetrievalDataset,
    load_retrieval_dataset,
    run_retrieval_benchmark,
)
from rag_lab.chunking import TextDocument
from rag_lab.providers import DeterministicFakeEmbeddingProvider


def test_fixed_dataset_is_valid_and_has_multiple_cases() -> None:
    dataset = load_retrieval_dataset()

    assert DEFAULT_DATASET_PATH.name == "retrieval-baseline.json"
    assert len(dataset.documents) == 11
    assert len(dataset.cases) == 13


def test_fake_baseline_runs_without_network() -> None:
    result = run_retrieval_benchmark(
        load_retrieval_dataset(),
        DeterministicFakeEmbeddingProvider(),
        top_k=3,
    )

    assert result.case_count == 13
    assert 0.0 <= result.recall_at_k <= 1.0
    assert 0.0 <= result.mean_reciprocal_rank <= 1.0
    assert all(len(case.hit_citations) == 3 for case in result.cases)
    assert result.as_dict()["case_count"] == 13


def test_dataset_rejects_case_that_references_unknown_path() -> None:
    with pytest.raises(ValueError, match="unknown document paths"):
        RetrievalDataset(
            documents=(TextDocument(root_id="docs", path="known.md", text="known"),),
            cases=(
                RetrievalCase(
                    case_id="missing",
                    query="where",
                    relevant_paths=("missing.md",),
                ),
            ),
        )


def test_dataset_loader_rejects_invalid_json(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"root_id": "docs"}), encoding="utf-8")

    with pytest.raises(ValueError, match="documents"):
        load_retrieval_dataset(path)


@pytest.mark.parametrize("top_k", [True, 0, -1])
def test_benchmark_rejects_invalid_top_k(top_k: int) -> None:
    with pytest.raises(ValueError, match="top_k"):
        run_retrieval_benchmark(
            load_retrieval_dataset(),
            DeterministicFakeEmbeddingProvider(),
            top_k=top_k,
        )
