from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from rag_lab.citations import CitationContext
from rag_lab.faithfulness import (
    ClaimVerdict,
    FaithfulnessError,
    OpenAIFaithfulnessJudge,
    evaluate_faithfulness,
)


def _context() -> CitationContext:
    return CitationContext(
        text=(
            "UNTRUSTED RETRIEVED EVIDENCE\n"
            "[SOURCE 1] docs:guide.md:1-2\nEvaluation verifies behavior."
        ),
        citations=("docs:guide.md:1-2",),
        hit_count=1,
        truncated=False,
    )


class StubJudge:
    def judge(self, *, answer, context):  # noqa: ANN001, ANN201, ARG002
        return [
            ClaimVerdict(
                claim="Evaluation verifies behavior.",
                supported=True,
                citations=(context.citations[0],),
                reason="The evidence states this directly.",
            ),
            ClaimVerdict(
                claim="Evaluation has 100 cases.",
                supported=False,
                citations=(),
                reason="The evidence gives no case count.",
            ),
        ]


def test_faithfulness_scores_atomic_claim_verdicts() -> None:
    report = evaluate_faithfulness("answer", _context(), StubJudge())

    assert report.total_claims == 2
    assert report.supported_claims == 1
    assert report.score == 0.5
    assert report.as_dict()["score"] == 0.5


def test_faithfulness_without_factual_claims_is_not_applicable() -> None:
    class EmptyJudge:
        def judge(self, *, answer, context):  # noqa: ANN001, ANN201, ARG002
            return []

    report = evaluate_faithfulness("I do not know.", _context(), EmptyJudge())

    assert report.score is None
    assert report.total_claims == 0


def test_supported_claim_requires_evidence_citation() -> None:
    with pytest.raises(ValueError, match="must cite evidence"):
        ClaimVerdict(
            claim="supported",
            supported=True,
            citations=(),
            reason="claimed support",
        )


def test_openai_judge_parses_structured_claims() -> None:
    calls = []
    payload = {
        "claims": [
            {
                "claim": "Evaluation verifies behavior.",
                "supported": True,
                "citations": ["docs:guide.md:1-2"],
                "reason": "Directly stated.",
            }
        ]
    }

    class FakeCompletions:
        def create(self, **kwargs):  # noqa: ANN003, ANN201
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(payload))
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    judge = OpenAIFaithfulnessJudge(client, model="judge-test")

    verdicts = judge.judge(answer="answer", context=_context())

    assert verdicts[0].supported is True
    assert verdicts[0].citations == ("docs:guide.md:1-2",)
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert _context().text in calls[0]["messages"][1]["content"]


def test_openai_judge_rejects_citation_outside_evidence() -> None:
    payload = {
        "claims": [
            {
                "claim": "Invented.",
                "supported": True,
                "citations": ["docs:invented.md:1-1"],
                "reason": "Not actually supported.",
            }
        ]
    }

    class FakeCompletions:
        def create(self, **kwargs):  # noqa: ANN003, ANN201, ARG002
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(payload))
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    with pytest.raises(FaithfulnessError, match="outside retrieved evidence"):
        OpenAIFaithfulnessJudge(client, model="judge-test").judge(
            answer="answer",
            context=_context(),
        )


@pytest.mark.parametrize("content", ["not json", "[]", '{"missing":[]}'])
def test_openai_judge_rejects_invalid_payload(content: str) -> None:
    class FakeCompletions:
        def create(self, **kwargs):  # noqa: ANN003, ANN201, ARG002
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    with pytest.raises(FaithfulnessError):
        OpenAIFaithfulnessJudge(client, model="judge-test").judge(
            answer="answer",
            context=_context(),
        )
