from __future__ import annotations

import codecs
from concurrent.futures import CancelledError
from pathlib import Path

import pytest
from workbench_core.files import FileSearchService, WorkspaceError
from workbench_core.files import service as file_service_module


def test_file_search_contract_and_cross_root_search(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    prompts = tmp_path / "prompts"
    notes.mkdir()
    prompts.mkdir()
    (notes / "style.md").write_text("first line\nCinematic portrait\n", encoding="utf-8")
    (prompts / "tifa.txt").write_text("Tifa character prompt\n", encoding="utf-8")

    service = FileSearchService({"notes": notes, "prompts": prompts})

    assert service.root_ids == ["notes", "prompts"]
    root_list = service.list_files()
    assert root_list["ok"] is True
    assert [entry["name"] for entry in root_list["entries"]] == ["notes", "prompts"]

    listing = service.list_files("notes")
    assert listing["ok"] is True
    assert listing["untrusted_content"] is True
    assert listing["citation"] == "notes:."
    assert listing["entries"] == [{"name": "style.md", "type": "file"}]

    read = service.read_file("notes", "style.md")
    assert read["ok"] is True
    assert read["citation"] == "notes:style.md:1-2"
    assert read["content"] == "first line\nCinematic portrait"

    search = service.search("tifa")
    assert search["ok"] is True
    assert search["root_ids"] == ["notes", "prompts"]
    assert {match["match_type"] for match in search["matches"]} == {
        "filename",
        "content",
    }
    assert all(match["root_id"] == "prompts" for match in search["matches"])
    assert all(match["untrusted_content"] is True for match in search["matches"])
    assert str(tmp_path) not in repr(listing)
    assert str(tmp_path) not in repr(read)
    assert str(tmp_path) not in repr(search)


def test_root_bindings_are_opaque_stable_copies(tmp_path: Path) -> None:
    same_root = FileSearchService({"docs": tmp_path})
    same_root_again = FileSearchService({"docs": tmp_path / "."})
    other = tmp_path / "other"
    other.mkdir()
    other_root = FileSearchService({"docs": other})

    bindings = same_root.root_bindings

    assert bindings == same_root_again.root_bindings
    assert bindings != other_root.root_bindings
    assert set(bindings) == {"docs"}
    assert len(bindings["docs"]) == 64
    assert str(tmp_path) not in repr(bindings)
    bindings["docs"] = "mutated"
    assert bindings != same_root.root_bindings


@pytest.mark.parametrize(
    ("relative_path", "error_code"),
    [
        ("../outside", "path_outside_root"),
        ("C:relative", "invalid_path"),
        ("\\Windows\\system.ini", "invalid_path"),
        ("safe.txt:secret", "invalid_path"),
    ],
)
def test_windows_and_containment_paths_are_rejected(
    tmp_path: Path,
    relative_path: str,
    error_code: str,
) -> None:
    service = FileSearchService({"root": tmp_path})

    result = service.read_file("root", relative_path)

    assert result["ok"] is False
    assert result["error_code"] == error_code
    assert str(tmp_path) not in repr(result)


def test_sensitive_paths_are_hidden_and_blocked(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / "cache.db").write_bytes(b"SQLite format 3\x00")
    (tmp_path / "id_rsa").write_text("private", encoding="utf-8")
    (tmp_path / "database.sqlite3").write_bytes(b"SQLite format 3\x00")
    (tmp_path / "database-wal").write_bytes(b"wal")
    (tmp_path / "database-shm").write_bytes(b"shm")
    (tmp_path / "database-journal").write_bytes(b"journal")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("credential", encoding="utf-8")
    (tmp_path / "public.md").write_text("safe", encoding="utf-8")
    service = FileSearchService({"root": tmp_path})

    listing = service.list_files("root")
    assert {entry["name"] for entry in listing["entries"]} == {"public.md"}
    for path in (
        ".env",
        "cache.db",
        "id_rsa",
        "database.sqlite3",
        "database-wal",
        "database-shm",
        "database-journal",
        ".git/config",
    ):
        result = service.read_file("root", path)
        assert result["error_code"] == "blocked_path"

    search = service.search("secret", root_id="root")
    assert search["ok"] is True
    assert search["matches"] == []


def test_binary_signatures_and_private_key_markers_are_blocked(tmp_path: Path) -> None:
    (tmp_path / "renamed-archive.txt").write_bytes(b"PK\x03\x04payload")
    (tmp_path / "renamed-key.txt").write_text(
        "header\n-----BEGIN PRIVATE KEY-----\nsecret\n",
        encoding="utf-8",
    )
    service = FileSearchService({"root": tmp_path})

    assert service.read_file("root", "renamed-archive.txt")["error_code"] == "binary_file"
    assert service.read_file("root", "renamed-key.txt")["error_code"] == "sensitive_content"


def test_read_file_rejects_replacement_between_validation_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("safe original", encoding="utf-8")
    replacement = tmp_path.parent / f"{tmp_path.name}-replacement.txt"
    replacement.write_text(
        "-----BEGIN PRIVATE KEY-----\nreplacement must not be read",
        encoding="utf-8",
    )
    target_path = target.resolve()
    original_open = Path.open
    swapped = False

    def swap_before_open(path: Path, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal swapped
        if path == target_path and not swapped:
            replacement.replace(target_path)
            swapped = True
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swap_before_open)
    service = FileSearchService({"root": tmp_path})

    result = service.read_file("root", "note.txt")

    assert swapped is True
    assert result == {
        "ok": False,
        "error_code": "path_changed",
        "error": "The requested file changed.",
    }
    assert "replacement must not be read" not in repr(result)


def test_search_fails_closed_when_candidate_changes_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("safe original", encoding="utf-8")
    replacement = tmp_path.parent / f"{tmp_path.name}-search-replacement.txt"
    replacement.write_text("needle from replacement", encoding="utf-8")
    target_path = target.resolve()
    original_open = Path.open
    swapped = False

    def swap_before_open(path: Path, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal swapped
        if path == target_path and not swapped:
            replacement.replace(target_path)
            swapped = True
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swap_before_open)
    service = FileSearchService({"root": tmp_path})

    result = service.search("needle", root_id="root")

    assert swapped is True
    assert result == {
        "ok": False,
        "error_code": "path_changed",
        "error": "A searched file changed during validation.",
    }
    assert "needle from replacement" not in repr(result)


def test_search_budget_counts_invalid_and_binary_reads(tmp_path: Path, monkeypatch) -> None:
    for index in range(3):
        (tmp_path / f"invalid-{index}.txt").write_bytes(b"\xff" * 4)
    monkeypatch.setattr(file_service_module, "MAX_SEARCH_BYTES", 6)
    service = FileSearchService({"root": tmp_path})

    result = service.search("does-not-match", root_id="root")

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["truncation_reason"] == "byte_budget"
    assert result["scanned_bytes"] == 4
    assert result["skipped_files"] == 1


def test_search_directory_budget_is_reported(tmp_path: Path, monkeypatch) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "note.txt").write_text("needle", encoding="utf-8")
    monkeypatch.setattr(file_service_module, "MAX_SEARCH_DIRECTORIES", 1)
    service = FileSearchService({"root": tmp_path})

    result = service.search("needle", root_id="root")

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["truncation_reason"] == "directory_budget"


def test_search_cooperatively_cancels_during_directory_scan(tmp_path: Path) -> None:
    for index in range(10):
        (tmp_path / f"note-{index}.txt").write_text("no match", encoding="utf-8")
    service = FileSearchService({"root": tmp_path})
    checks = 0

    def cancel_check() -> None:
        nonlocal checks
        checks += 1
        if checks == 5:
            raise CancelledError("stop file search")

    with pytest.raises(CancelledError, match="stop file search"):
        service.search("needle", root_id="root", cancel_check=cancel_check)

    assert checks == 5


def test_root_listing_honors_max_entries(tmp_path: Path) -> None:
    roots = {name: tmp_path for name in ("one", "two", "three")}
    service = FileSearchService(roots)

    result = service.list_files(max_entries=1)

    assert result["entries"] == [{"name": "one", "path": "one", "type": "root"}]
    assert result["truncated"] is True


def test_read_file_enforces_type_encoding_size_and_response_limits(tmp_path: Path) -> None:
    (tmp_path / "binary.txt").write_bytes(b"hello\x00world")
    (tmp_path / "invalid.txt").write_bytes(b"\xff\xfe\x00")
    (tmp_path / "image.png").write_bytes(b"not an image")
    (tmp_path / "large.txt").write_bytes(b"x" * (1024 * 1024 + 1))
    (tmp_path / "long.txt").write_text("x" * 20_000, encoding="utf-8")
    utf16_payload = codecs.BOM_UTF16_LE + "hello".encode("utf-16-le")
    (tmp_path / "utf16.txt").write_bytes(utf16_payload)
    service = FileSearchService({"root": tmp_path})

    assert service.read_file("root", "binary.txt")["error_code"] == "binary_file"
    assert service.read_file("root", "invalid.txt")["error_code"] == "unsupported_encoding"
    assert service.read_file("root", "image.png")["error_code"] == "unsupported_file_type"
    assert service.read_file("root", "large.txt")["error_code"] == "file_too_large"
    assert service.read_file("root", "utf16.txt")["content"] == "hello"

    capped = service.read_file("root", "long.txt")
    assert capped["ok"] is True
    assert capped["chars"] == 16_000
    assert capped["truncated"] is True
    assert capped["truncation"]["response_char_limit"] is True


def test_handlers_revalidate_numeric_limits_and_root_ids(tmp_path: Path) -> None:
    service = FileSearchService({"root": tmp_path})

    assert service.list_files("root", max_entries=True)["error_code"] == "invalid_argument"
    assert service.search("x", max_results=51)["error_code"] == "invalid_argument"
    assert service.read_file("root", "missing.txt", max_lines=201)["error_code"] == (
        "invalid_argument"
    )
    assert service.read_file("missing", "file.txt")["error_code"] == "unknown_root"

    with pytest.raises(WorkspaceError):
        FileSearchService({"Docs": tmp_path, "docs": tmp_path})
    with pytest.raises(WorkspaceError):
        FileSearchService({"bad.root": tmp_path})


def test_symlink_is_skipped_and_cannot_be_read(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this host")
    service = FileSearchService({"root": tmp_path})

    listing = service.list_files("root")
    assert "linked.txt" not in {entry["name"] for entry in listing["entries"]}
    blocked = service.read_file("root", "linked.txt")
    assert blocked["error_code"] == "blocked_path"
