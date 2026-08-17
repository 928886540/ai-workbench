from __future__ import annotations

import pytest
from rag_lab.chunking import TextDocument, chunk_document


def test_chunk_document_keeps_text_and_line_citation() -> None:
    document = TextDocument(
        root_id="docs",
        path="guide.md",
        text="alpha\nbeta",
    )

    chunks = chunk_document(document, max_chars=100)

    assert len(chunks) == 1
    assert chunks[0].chunk_id == "docs:guide.md#chunk-0"
    assert chunks[0].text == "alpha\nbeta"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 2
    assert chunks[0].citation == "docs:guide.md:1-2"


def test_chunk_document_prefers_complete_lines() -> None:
    document = TextDocument(
        root_id="docs",
        path="guide.txt",
        text="alpha\nbeta\ngamma",
    )

    chunks = chunk_document(document, max_chars=10)

    assert [chunk.text for chunk in chunks] == ["alpha", "beta\ngamma"]
    assert [chunk.citation for chunk in chunks] == [
        "docs:guide.txt:1-1",
        "docs:guide.txt:2-3",
    ]


def test_chunk_document_hard_splits_one_long_line() -> None:
    document = TextDocument(
        root_id="docs",
        path="data.json",
        text="abcdefghijkl",
    )

    chunks = chunk_document(document, max_chars=5)

    assert [chunk.text for chunk in chunks] == ["abcde", "fghij", "kl"]
    assert all(chunk.citation == "docs:data.json:1-1" for chunk in chunks)
    assert all(len(chunk.text) <= 5 for chunk in chunks)


def test_chunk_document_normalizes_windows_line_endings() -> None:
    document = TextDocument(
        root_id="docs",
        path="guide.md",
        text="alpha\r\nbeta\rcharlie",
    )

    chunks = chunk_document(document, max_chars=100)

    assert chunks[0].text == "alpha\nbeta\ncharlie"
    assert chunks[0].citation == "docs:guide.md:1-3"


def test_chunk_document_ignores_blank_document() -> None:
    document = TextDocument(root_id="docs", path="empty.txt", text=" \n\n")

    assert chunk_document(document) == []


@pytest.mark.parametrize(
    ("root_id", "path"),
    [
        ("", "guide.md"),
        ("docs", "../secret.txt"),
        ("docs", "/absolute/guide.md"),
        ("docs", "guide.pdf"),
    ],
)
def test_document_rejects_invalid_source_identity(root_id: str, path: str) -> None:
    with pytest.raises(ValueError):
        TextDocument(root_id=root_id, path=path, text="content")


@pytest.mark.parametrize("max_chars", [True, 0, -1])
def test_chunk_document_rejects_invalid_size(max_chars: int) -> None:
    document = TextDocument(root_id="docs", path="guide.md", text="content")

    with pytest.raises(ValueError):
        chunk_document(document, max_chars=max_chars)
