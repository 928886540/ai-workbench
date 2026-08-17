"""Deterministic text chunking with root-relative citations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

SUPPORTED_SUFFIXES = frozenset({".json", ".md", ".txt"})
_ROOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class TextDocument:
    """One already-authorized UTF-8 text document."""

    root_id: str
    path: str
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.root_id, str) or not _ROOT_ID_PATTERN.fullmatch(
            self.root_id
        ):
            raise ValueError("root_id must be a non-empty safe identifier")
        if not isinstance(self.path, str):
            raise ValueError("path must be a root-relative string")
        normalized_path = self.path.strip().replace("\\", "/")
        parsed_path = PurePosixPath(normalized_path)
        if (
            not normalized_path
            or normalized_path == "."
            or parsed_path.is_absolute()
            or ".." in parsed_path.parts
            or ":" in normalized_path
        ):
            raise ValueError("path must stay relative to its configured root")
        if parsed_path.suffix.casefold() not in SUPPORTED_SUFFIXES:
            raise ValueError("only Markdown, TXT, and JSON documents are supported")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        object.__setattr__(self, "path", parsed_path.as_posix())


@dataclass(frozen=True, slots=True)
class Chunk:
    """A bounded evidence unit whose citation never exposes an absolute path."""

    chunk_id: str
    root_id: str
    path: str
    ordinal: int
    text: str
    start_line: int
    end_line: int
    citation: str
    untrusted_content: bool = field(default=True, init=False)


def _line_units(text: str, max_chars: int) -> list[tuple[str, int]]:
    lines = text.split("\n")
    units: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        line_number = index + 1
        value = line + ("\n" if index < len(lines) - 1 else "")
        for offset in range(0, len(value), max_chars):
            units.append((value[offset : offset + max_chars], line_number))
    return units


def chunk_document(document: TextDocument, *, max_chars: int = 800) -> list[Chunk]:
    """Split text deterministically, preferring line boundaries when they fit."""

    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")

    normalized_text = document.text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized_text.strip():
        return []

    chunks: list[Chunk] = []
    parts: list[str] = []
    size = 0
    start_line = 0
    end_line = 0

    def flush() -> None:
        nonlocal parts, size, start_line, end_line
        text = "".join(parts).rstrip("\n")
        if text.strip():
            ordinal = len(chunks)
            citation = (
                f"{document.root_id}:{document.path}:{start_line}-{end_line}"
            )
            chunks.append(
                Chunk(
                    chunk_id=(
                        f"{document.root_id}:{document.path}#chunk-{ordinal}"
                    ),
                    root_id=document.root_id,
                    path=document.path,
                    ordinal=ordinal,
                    text=text,
                    start_line=start_line,
                    end_line=end_line,
                    citation=citation,
                )
            )
        parts = []
        size = 0
        start_line = 0
        end_line = 0

    for value, line_number in _line_units(normalized_text, max_chars):
        if parts and size + len(value) > max_chars:
            flush()
        if not parts:
            start_line = line_number
        parts.append(value)
        size += len(value)
        end_line = line_number
        if size == max_chars:
            flush()
    if parts:
        flush()
    return chunks
