from __future__ import annotations

from types import SimpleNamespace

import pytest
from rag_lab.citations import CitationContext
from rag_lab.generation import (
    AnswerGenerationError,
    DeterministicFakeAnswerGenerator,
    OpenAIAnswerGenerator,
    extract_citations,
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


def test_extract_citations_preserves_order_and_deduplicates() -> None:
    citations = extract_citations(
        "First [CITE:docs:guide.md:1-2] repeat [CITE:docs:guide.md:1-2] "
        "then [CITE:notes:plan.md:4-6]."
    )

    assert citations == ("docs:guide.md:1-2", "notes:plan.md:4-6")


def test_extract_citations_ignores_malformed_labels() -> None:
    assert extract_citations("[CITE:absolute:C:/secret.txt:1-2] [SOURCE 1]") == ()


def test_fake_answer_generator_cites_retrieved_evidence() -> None:
    answer = DeterministicFakeAnswerGenerator().generate(
        query="What is evaluated?",
        context=_context(),
    )

    assert "[CITE:docs:guide.md:1-2]" in answer


def test_openai_answer_generator_builds_grounded_prompt() -> None:
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):  # noqa: ANN003, ANN201
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="Evaluation checks behavior. [CITE:docs:guide.md:1-2]"
                        )
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    generator = OpenAIAnswerGenerator(client, model="chat-test")

    answer = generator.generate(query="What is evaluated?", context=_context())

    assert answer.endswith("[CITE:docs:guide.md:1-2]")
    assert calls[0]["model"] == "chat-test"
    assert calls[0]["temperature"] == 0
    assert "untrusted data" in calls[0]["messages"][0]["content"]
    assert "question is a request, not evidence" in calls[0]["messages"][0]["content"]
    assert "Do not expand abbreviations" in calls[0]["messages"][0]["content"]
    assert _context().text in calls[0]["messages"][1]["content"]


def test_openai_answer_generator_wraps_provider_error() -> None:
    class BrokenCompletions:
        def create(self, **kwargs):  # noqa: ANN003, ANN201, ARG002
            raise OSError("provider unavailable")

    client = SimpleNamespace(chat=SimpleNamespace(completions=BrokenCompletions()))

    with pytest.raises(AnswerGenerationError, match="request failed"):
        OpenAIAnswerGenerator(client, model="chat-test").generate(
            query="question",
            context=_context(),
        )


def test_openai_answer_generator_rejects_empty_response() -> None:
    class EmptyCompletions:
        def create(self, **kwargs):  # noqa: ANN003, ANN201, ARG002
            return SimpleNamespace(choices=[])

    client = SimpleNamespace(chat=SimpleNamespace(completions=EmptyCompletions()))

    with pytest.raises(AnswerGenerationError, match="no choices"):
        OpenAIAnswerGenerator(client, model="chat-test").generate(
            query="question",
            context=_context(),
        )
