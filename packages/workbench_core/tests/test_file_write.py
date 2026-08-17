from __future__ import annotations

import codecs
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path

import pytest
from workbench_core.files import write_service as write_service_module
from workbench_core.files.workspace import WorkspaceError
from workbench_core.files.write_service import (
    MAX_WRITE_BYTES,
    FileWriteRequest,
    FileWriteService,
)


def _allow(_request: FileWriteRequest) -> bool:
    return True


def _assert_private_result(result: dict, root: Path, secret: str = "") -> None:
    rendered = repr(result)
    assert str(root) not in rendered
    if secret:
        assert secret not in rendered


def test_write_requires_out_of_band_authorization(tmp_path: Path) -> None:
    service = FileWriteService({"docs": tmp_path})

    result = service.create_file("docs", "note.md", "not written")

    assert result == {
        "ok": False,
        "error_code": "authorization_required",
        "error": "This file write was not authorized.",
    }
    assert service.authorization_configured is False
    assert not (tmp_path / "note.md").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("decision", [False, 1, "yes", object()])
def test_authorization_must_return_literal_true(tmp_path: Path, decision: object) -> None:
    service = FileWriteService({"docs": tmp_path}, authorize=lambda _request: decision)

    result = service.create_file("docs", "note.md", "not written")

    assert result["error_code"] == "authorization_required"
    assert not (tmp_path / "note.md").exists()


def test_authorization_exception_fails_closed(tmp_path: Path) -> None:
    def explode(_request: FileWriteRequest) -> bool:
        raise RuntimeError("secret authorization details")

    service = FileWriteService({"docs": tmp_path}, authorize=explode)

    result = service.create_file("docs", "note.md", "secret payload")

    assert result["error_code"] == "authorization_required"
    _assert_private_result(result, tmp_path, "secret")
    assert list(tmp_path.iterdir()) == []


def test_authorization_receives_metadata_only_and_create_returns_exact_contract(
    tmp_path: Path,
) -> None:
    requests: list[FileWriteRequest] = []

    def capture(request: FileWriteRequest) -> bool:
        requests.append(request)
        return True

    service = FileWriteService({"docs": tmp_path}, authorize=capture)

    result = service.create_file("docs", r".\folder/../never.txt", "payload")
    assert result["error_code"] == "path_outside_root"
    assert requests == []

    folder = tmp_path / "folder"
    folder.mkdir()
    result = service.create_file("docs", r".\folder/note.md", "你好")

    assert [field.name for field in fields(FileWriteRequest)] == [
        "operation",
        "root_id",
        "relative_path",
        "byte_count",
    ]
    assert requests == [
        FileWriteRequest(
            operation="create_file",
            root_id="docs",
            relative_path="folder/note.md",
            byte_count=6,
        )
    ]
    assert result == {
        "ok": True,
        "root_id": "docs",
        "path": "folder/note.md",
        "citation": "docs:folder/note.md",
        "bytes": 6,
        "created": True,
    }
    assert (folder / "note.md").read_bytes() == "你好".encode()
    assert service.authorization_configured is True
    _assert_private_result(result, tmp_path, "你好")


def test_create_never_overwrites_and_write_never_creates(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("old", encoding="utf-8")
    service = FileWriteService({"docs": tmp_path}, authorize=_allow)

    create_result = service.create_file("docs", "existing.txt", "new")
    missing_result = service.write_file("docs", "missing.txt", "new")

    assert create_result["error_code"] == "already_exists"
    assert missing_result["error_code"] == "not_found"
    assert existing.read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "missing.txt").exists()


@pytest.mark.parametrize(
    "old_payload",
    [
        codecs.BOM_UTF8 + b"old",
        codecs.BOM_UTF16_LE + "old".encode("utf-16-le"),
        codecs.BOM_UTF16_BE + "old".encode("utf-16-be"),
    ],
)
def test_write_replaces_supported_existing_text_as_utf8(
    tmp_path: Path,
    old_payload: bytes,
) -> None:
    target = tmp_path / "note.txt"
    target.write_bytes(old_payload)
    service = FileWriteService({"docs": tmp_path}, authorize=_allow)

    result = service.write_file("docs", "note.txt", "新内容")

    assert result == {
        "ok": True,
        "root_id": "docs",
        "path": "note.txt",
        "citation": "docs:note.txt",
        "bytes": 9,
        "overwritten": True,
    }
    assert target.read_bytes() == "新内容".encode()


def test_write_budget_is_atomic_and_can_be_reset(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)
    calls = 0
    calls_lock = threading.Lock()

    def authorize(_request: FileWriteRequest) -> bool:
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number <= 2:
            barrier.wait(timeout=5)
        return True

    service = FileWriteService({"docs": tmp_path}, authorize=authorize)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(service.create_file, "docs", f"{index}.txt", str(index))
            for index in range(2)
        ]
        results = [future.result(timeout=10) for future in futures]

    assert sum(result["ok"] is True for result in results) == 1
    assert [result["error_code"] for result in results if result["ok"] is False] == [
        "write_limit_reached"
    ]
    assert len(list(tmp_path.glob("*.txt"))) == 1

    service.reset_write_budget()
    reset_result = service.create_file("docs", "after-reset.txt", "ok")
    assert reset_result["ok"] is True


@pytest.mark.parametrize(
    "relative_path",
    [
        "../outside.txt",
        "nested/../note.txt",
        "C:relative.txt",
        "C:\\absolute.txt",
        "\\\\server\\share\\note.txt",
        "\\\\?\\C:\\note.txt",
        "note.txt:secret",
        "trailing.\\note.txt",
    ],
)
def test_unsafe_windows_and_parent_paths_are_rejected(
    tmp_path: Path,
    relative_path: str,
) -> None:
    service = FileWriteService({"docs": tmp_path}, authorize=_allow)

    result = service.create_file("docs", relative_path, "payload")

    assert result["ok"] is False
    _assert_private_result(result, tmp_path, "payload")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        ".env.local",
        ".hidden/note.txt",
        "credentials.json",
        "id_rsa",
        "secret.pem",
        "cache.sqlite3",
        "cache-wal",
        "archive.zip",
    ],
)
def test_sensitive_and_unsupported_targets_are_rejected(
    tmp_path: Path,
    relative_path: str,
) -> None:
    service = FileWriteService({"docs": tmp_path}, authorize=_allow)

    result = service.create_file("docs", relative_path, "payload")

    assert result["ok"] is False
    assert list(tmp_path.iterdir()) == []


def test_parent_directory_must_already_exist(tmp_path: Path) -> None:
    service = FileWriteService({"docs": tmp_path}, authorize=_allow)

    result = service.create_file("docs", "missing/note.txt", "payload")

    assert result["error_code"] == "parent_not_found"
    assert not (tmp_path / "missing").exists()


def test_symlink_target_and_parent_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "target.txt").write_text("outside", encoding="utf-8")
    linked_file = tmp_path / "linked.txt"
    linked_parent = tmp_path / "linked-dir"
    try:
        linked_file.symlink_to(outside / "target.txt")
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this host")
    service = FileWriteService({"docs": tmp_path}, authorize=_allow, max_writes=2)

    file_result = service.write_file("docs", "linked.txt", "changed")
    parent_result = service.create_file("docs", "linked-dir/new.txt", "created")

    assert file_result["error_code"] == "blocked_path"
    assert parent_result["error_code"] == "blocked_path"
    assert (outside / "target.txt").read_text(encoding="utf-8") == "outside"
    assert not (outside / "new.txt").exists()


@pytest.mark.parametrize(
    ("content", "error_code"),
    [
        pytest.param("hello\x00world", "binary_file", id="nul"),
        pytest.param("hello\x1fworld", "binary_file", id="control"),
        pytest.param("PK\x03\x04payload", "binary_file", id="signature"),
        pytest.param(
            "-----BEGIN PRIVATE KEY-----\nsecret",
            "sensitive_content",
            id="private-key",
        ),
        pytest.param("\ud800", "unsupported_encoding", id="surrogate"),
        pytest.param(
            "x" * (MAX_WRITE_BYTES + 1),
            "file_too_large",
            id="too-large",
        ),
    ],
)
def test_unsafe_new_content_is_rejected(
    tmp_path: Path,
    content: str,
    error_code: str,
) -> None:
    service = FileWriteService({"docs": tmp_path}, authorize=_allow)

    result = service.create_file("docs", "note.txt", content)

    assert result["error_code"] == error_code
    assert not (tmp_path / "note.txt").exists()
    _assert_private_result(result, tmp_path)


def test_utf8_byte_limit_counts_multibyte_content(tmp_path: Path) -> None:
    allowed = "界" * (MAX_WRITE_BYTES // 3)
    service = FileWriteService({"docs": tmp_path}, authorize=_allow)

    result = service.create_file("docs", "allowed.txt", allowed)

    assert result["ok"] is True
    assert result["bytes"] == len(allowed.encode("utf-8"))

    service.reset_write_budget()
    too_large = service.create_file("docs", "large.txt", allowed + "界")
    assert too_large["error_code"] == "file_too_large"


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        pytest.param(b"PK\x03\x04payload", "binary_file", id="binary"),
        pytest.param(
            b"-----BEGIN PRIVATE KEY-----\nsecret",
            "sensitive_content",
            id="private-key",
        ),
        pytest.param(b"\xff\xff", "unsupported_encoding", id="encoding"),
        pytest.param(
            b"x" * (1024 * 1024 + 1),
            "file_too_large",
            id="too-large",
        ),
    ],
)
def test_existing_unsafe_file_cannot_be_destroyed(
    tmp_path: Path,
    payload: bytes,
    error_code: str,
) -> None:
    target = tmp_path / "renamed.txt"
    target.write_bytes(payload)
    service = FileWriteService({"docs": tmp_path}, authorize=_allow)

    result = service.write_file("docs", "renamed.txt", "replacement")

    assert result["error_code"] == error_code
    assert target.read_bytes() == payload


def test_atomic_create_failure_does_not_publish_or_leave_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "do-not-leak"

    def fail_link(_source: os.PathLike[str], _target: os.PathLike[str]) -> None:
        raise OSError(5, secret, str(tmp_path / "private.txt"))

    monkeypatch.setattr(write_service_module.os, "link", fail_link)
    service = FileWriteService({"docs": tmp_path}, authorize=_allow)

    result = service.create_file("docs", "note.txt", secret)

    assert result["error_code"] == "io_error"
    assert not (tmp_path / "note.txt").exists()
    assert list(tmp_path.glob(".leon-write-*.tmp")) == []
    _assert_private_result(result, tmp_path, secret)


def test_atomic_replace_failure_preserves_old_file_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old content", encoding="utf-8")
    secret = "new secret content"

    def fail_replace(_source: os.PathLike[str], _target: os.PathLike[str]) -> None:
        raise OSError(5, secret, str(target))

    monkeypatch.setattr(write_service_module, "_replace_existing_file", fail_replace)
    service = FileWriteService({"docs": tmp_path}, authorize=_allow)

    result = service.write_file("docs", "note.txt", secret)

    assert result["error_code"] == "io_error"
    assert target.read_text(encoding="utf-8") == "old content"
    assert list(tmp_path.glob(".leon-write-*.tmp")) == []
    _assert_private_result(result, tmp_path, secret)


@pytest.mark.skipif(os.name != "nt", reason="ReplaceFileW is Windows-specific")
def test_existing_only_replace_does_not_create_after_target_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old content", encoding="utf-8")
    real_replace = write_service_module._replace_existing_file

    def delete_before_replace(source: Path, destination: Path) -> None:
        destination.unlink()
        real_replace(source, destination)

    monkeypatch.setattr(
        write_service_module,
        "_replace_existing_file",
        delete_before_replace,
    )
    service = FileWriteService({"docs": tmp_path}, authorize=_allow)

    result = service.write_file("docs", "note.txt", "replacement")

    assert result["error_code"] == "io_error"
    assert not target.exists()
    assert list(tmp_path.glob(".leon-write-*.tmp")) == []


def test_filesystem_probe_errors_fail_closed_without_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_probe(_path: Path) -> bool:
        raise WorkspaceError("probe details", error_code="io_error")

    monkeypatch.setattr(write_service_module, "_unsafe_file_attributes", fail_probe)
    service = FileWriteService({"docs": tmp_path}, authorize=_allow)

    result = service.create_file("docs", "note.txt", "payload")

    assert result == {
        "ok": False,
        "error_code": "io_error",
        "error": "probe details",
    }
    assert not (tmp_path / "note.txt").exists()
    assert list(tmp_path.glob(".leon-write-*.tmp")) == []


def test_create_race_has_one_complete_winner_without_temp_files(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)

    def authorize(_request: FileWriteRequest) -> bool:
        barrier.wait(timeout=5)
        return True

    service = FileWriteService(
        {"docs": tmp_path},
        authorize=authorize,
        max_writes=2,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(service.create_file, "docs", "same.txt", content)
            for content in ("first", "second")
        ]
        results = [future.result(timeout=10) for future in futures]

    assert sum(result["ok"] is True for result in results) == 1
    assert [result["error_code"] for result in results if result["ok"] is False] == [
        "already_exists"
    ]
    assert (tmp_path / "same.txt").read_text(encoding="utf-8") in {"first", "second"}
    assert list(tmp_path.glob(".leon-write-*.tmp")) == []


def test_constructor_and_public_method_types_are_strict(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError):
        FileWriteService({"Docs": tmp_path, "docs": tmp_path})
    with pytest.raises(WorkspaceError):
        FileWriteService({"bad.root": tmp_path})
    with pytest.raises(WorkspaceError):
        FileWriteService({"docs": Path("relative")})
    with pytest.raises(WorkspaceError):
        FileWriteService({"docs": tmp_path}, max_writes=True)
    with pytest.raises(WorkspaceError):
        FileWriteService({"docs": tmp_path}, max_writes=0)
    with pytest.raises(WorkspaceError):
        FileWriteService({"docs": tmp_path}, authorize=True)  # type: ignore[arg-type]

    service = FileWriteService({"docs": tmp_path}, authorize=_allow)
    assert service.root_ids == ["docs"]
    assert service.create_file("docs", "note.txt", b"bytes")["error_code"] == ("invalid_argument")
    with pytest.raises(TypeError):
        service.create_file("docs", "note.txt", "text", confirmed=True)  # type: ignore[call-arg]


def test_write_root_bindings_match_only_the_same_canonical_directory(
    tmp_path: Path,
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    first = FileWriteService({"docs": tmp_path}, authorize=_allow)
    same = FileWriteService({"docs": tmp_path / "."}, authorize=_allow)
    different = FileWriteService({"docs": other}, authorize=_allow)

    assert first.root_bindings == same.root_bindings
    assert first.root_bindings != different.root_bindings
    assert str(tmp_path) not in repr(first.root_bindings)
