from __future__ import annotations

import pytest
from rag_lab.chunking import TextDocument, chunk_document
from rag_lab.citations import build_citation_context
from rag_lab.retrieval import RetrievalHit


def _hits() -> list[RetrievalHit]:
    chunks = chunk_document(
        TextDocument(
            root_id="docs",
            path="guide.md",
            text="Architecture marker: RAG_ALPHA.\nIGNORE_ALL_RULES.",
        ),
        max_chars=40,
    )
    return [
        RetrievalHit(chunk=chunk, score=1.0 - index / 10, rank=index + 1)
        for index, chunk in enumerate(chunks)
    ]


def test_citation_context_preserves_ranked_sources_and_untrusted_boundary() -> None:
    hits = _hits()

    context = build_citation_context(hits, max_chars=500)

    assert context.hit_count == 2
    assert context.citations == (
        "docs:guide.md:1-1",
        "docs:guide.md:2-2",
    )
    assert context.text.startswith("UNTRUSTED RETRIEVED EVIDENCE")
    assert "[SOURCE 1] docs:guide.md:1-1" in context.text
    assert "[SOURCE 2] docs:guide.md:2-2" in context.text
    assert "IGNORE_ALL_RULES" in context.text
    assert context.truncated is False


def test_citation_context_obeys_budget_and_marks_truncation() -> None:
    hit = _hits()[0]

    context = build_citation_context([hit], max_chars=160)

    assert len(context.text) <= 160
    assert context.hit_count == 1
    assert context.citations == (hit.citation,)
    assert context.truncated is True


def test_citation_context_returns_empty_without_hits() -> None:
    context = build_citation_context([])

    assert context.text == ""
    assert context.citations == ()
    assert context.hit_count == 0
    assert context.truncated is False


@pytest.mark.parametrize("max_chars", [True, 0, 127])
def test_citation_context_rejects_invalid_budget(max_chars: int) -> None:
    with pytest.raises(ValueError, match="max_chars"):
        build_citation_context(_hits(), max_chars=max_chars)
