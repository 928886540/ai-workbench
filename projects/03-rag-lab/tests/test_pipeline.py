from __future__ import annotations

from rag_lab.chunking import TextDocument, chunk_document
from rag_lab.pipeline import answer_query
from rag_lab.retrieval import RetrievalHit


def _hit() -> RetrievalHit:
    chunk = chunk_document(
        TextDocument(
            root_id="docs",
            path="guide.md",
            text="Evaluation verifies Agent behavior.",
        )
    )[0]
    return RetrievalHit(chunk=chunk, score=0.9, rank=1)


class StubRetriever:
    def __init__(self, hits):  # noqa: ANN001
        self.hits = hits

    def retrieve(self, query, *, top_k):  # noqa: ANN001, ANN201, ARG002
        return self.hits[:top_k]


class StubGenerator:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = []

    def generate(self, *, query, context):  # noqa: ANN001, ANN201
        self.calls.append((query, context))
        return self.answer


def test_answer_query_scores_supported_and_invented_citations() -> None:
    hit = _hit()
    generator = StubGenerator(
        f"Supported [CITE:{hit.citation}] invented [CITE:docs:fake.md:1-1]"
    )

    result = answer_query(
        "What does Evaluation verify?",
        StubRetriever([hit]),
        generator,
    )

    assert result.citations == (hit.citation, "docs:fake.md:1-1")
    assert result.citation_precision == 0.5
    assert result.hits == (hit,)
    assert generator.calls[0][1].citations == (hit.citation,)
    assert result.as_dict()["citation_precision"] == 0.5
    assert result.faithfulness is None


def test_answer_query_does_not_generate_without_evidence() -> None:
    generator = StubGenerator("must not be used")

    result = answer_query("unknown", StubRetriever([]), generator)

    assert result.text == "Insufficient retrieved evidence."
    assert result.citations == ()
    assert result.citation_precision == 0.0
    assert result.faithfulness is None
    assert generator.calls == []


def test_answer_query_rejects_empty_generator_output() -> None:
    generator = StubGenerator("  ")

    try:
        answer_query("question", StubRetriever([_hit()]), generator)
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:
        raise AssertionError("empty generator output should fail")
