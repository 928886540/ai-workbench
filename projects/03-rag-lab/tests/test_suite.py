from __future__ import annotations

import pytest
from rag_lab.benchmark import load_retrieval_dataset
from rag_lab.faithfulness import ClaimVerdict
from rag_lab.generation import DeterministicFakeAnswerGenerator
from rag_lab.providers import DeterministicFakeEmbeddingProvider
from rag_lab.suite import run_rag_suite


class SupportingJudge:
    def judge(self, *, answer, context):  # noqa: ANN001, ANN201, ARG002
        return [
            ClaimVerdict(
                claim="The fake provider used retrieved evidence.",
                supported=True,
                citations=(context.citations[0],),
                reason="The selected evidence is present in context.",
            )
        ]


def test_fake_rag_suite_aggregates_all_metric_layers() -> None:
    result = run_rag_suite(
        load_retrieval_dataset(),
        DeterministicFakeEmbeddingProvider(),
        DeterministicFakeAnswerGenerator(),
        faithfulness_judge=SupportingJudge(),
        case_ids=["eval-vs-pytest", "rag-order"],
    )

    assert result.case_count == 2
    assert 0.0 <= result.recall_at_k <= 1.0
    assert 0.0 <= result.mean_reciprocal_rank <= 1.0
    assert result.citation_rate == 1.0
    assert result.mean_citation_precision == 1.0
    assert result.mean_faithfulness == 1.0
    assert result.as_dict()["judged_case_count"] == 2


def test_rag_suite_without_judge_reports_no_faithfulness() -> None:
    result = run_rag_suite(
        load_retrieval_dataset(),
        DeterministicFakeEmbeddingProvider(),
        DeterministicFakeAnswerGenerator(),
        case_ids=["provider-opt-in"],
    )

    assert result.faithfulness_scores == ()
    assert result.mean_faithfulness is None


def test_rag_suite_rejects_unknown_or_duplicate_case_ids() -> None:
    dataset = load_retrieval_dataset()
    provider = DeterministicFakeEmbeddingProvider()
    generator = DeterministicFakeAnswerGenerator()

    with pytest.raises(ValueError, match="unknown case ids"):
        run_rag_suite(dataset, provider, generator, case_ids=["missing"])
    with pytest.raises(ValueError, match="duplicates"):
        run_rag_suite(
            dataset,
            provider,
            generator,
            case_ids=["rag-order", "rag-order"],
        )
