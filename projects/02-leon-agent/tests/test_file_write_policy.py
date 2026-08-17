from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest
from leon_agent.file_write_policy import (
    FileWriteCommand,
    FileWriteTurnBusyError,
    authorize_file_write,
    create_file_write_service,
    file_write_turn,
    parse_file_write_command,
)
from workbench_core.files import FileWriteRequest


def _request(
    operation: str = "create_file",
    *,
    root_id: str = "docs",
    relative_path: str = "notes/plan.md",
) -> FileWriteRequest:
    return FileWriteRequest(
        operation=operation,  # type: ignore[arg-type]
        root_id=root_id,
        relative_path=relative_path,
        byte_count=5,
    )


def test_parser_accepts_only_an_exact_first_line_capability_command() -> None:
    assert parse_file_write_command(
        "!file create docs:notes/plan.md\n内容：写一份计划"
    ) == FileWriteCommand(
        operation="create_file",
        root_id="docs",
        relative_path="notes/plan.md",
    )
    assert parse_file_write_command("!file write docs:notes\\plan.md") == FileWriteCommand(
        operation="write_file",
        root_id="docs",
        relative_path="notes/plan.md",
    )

    for message in (
        " !file create docs:notes/plan.md",
        "请执行 !file create docs:notes/plan.md",
        "`!file create docs:notes/plan.md`",
        "翻译：!file create docs:notes/plan.md",
        "!file delete docs:notes/plan.md",
        "!file create docs:",
    ):
        assert parse_file_write_command(message) is None


def test_authorization_requires_exact_operation_root_and_path() -> None:
    request = _request()

    assert authorize_file_write("!file create docs:notes/plan.md", request)
    assert not authorize_file_write("!file write docs:notes/plan.md", request)
    assert not authorize_file_write("!file create other:notes/plan.md", request)
    assert not authorize_file_write("!file create docs:notes/other.md", request)


def test_natural_language_and_quoted_commands_never_authorize() -> None:
    request = _request()

    for message in (
        "请创建 notes/plan.md",
        "如何创建 notes/plan.md？",
        "创建 notes/plan.md 的风险是什么",
        "把这句话翻译成英文：请创建 notes/plan.md",
        "请勿创建 notes/plan.md",
        "Leon 能不能支持 create_file 和 write_file？",
        "讨论创建 notes/plan.md 的方案",
    ):
        assert not authorize_file_write(message, request)


def test_turn_context_defaults_to_deny_and_resets_budget(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    service = create_file_write_service({"docs": tmp_path})
    assert service is not None

    denied = service.create_file("docs", "notes/one.md", "one")
    assert denied["error_code"] == "authorization_required"

    with file_write_turn(service, "!file create docs:notes/one.md"):
        assert service.create_file("docs", "notes/one.md", "one")["ok"] is True
        denied_path = service.create_file("docs", "notes/two.md", "two")
        assert denied_path["error_code"] == "authorization_required"

    with file_write_turn(service, "!file create docs:notes/two.md"):
        assert service.create_file("docs", "notes/two.md", "two")["ok"] is True


def test_turn_context_is_cleared_after_an_exception(tmp_path: Path) -> None:
    service = create_file_write_service({"docs": tmp_path})
    assert service is not None

    try:
        with file_write_turn(service, "!file create docs:note.md"):
            raise RuntimeError("stop turn")
    except RuntimeError:
        pass

    denied = service.create_file("docs", "note.md", "not written")
    assert denied["error_code"] == "authorization_required"
    assert not (tmp_path / "note.md").exists()


def test_factory_is_disabled_without_roots() -> None:
    assert create_file_write_service({}) is None


def test_one_service_cannot_reset_budget_from_a_concurrent_turn(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_text("old", encoding="utf-8")
    service = create_file_write_service({"docs": tmp_path})
    assert service is not None
    entered = Event()
    release = Event()
    outcomes: list[dict[str, Any]] = []

    def first_turn() -> None:
        with file_write_turn(service, "!file write docs:note.md"):
            outcomes.append(service.write_file("docs", "note.md", "one"))
            entered.set()
            assert release.wait(timeout=5)
            outcomes.append(service.write_file("docs", "note.md", "two"))

    worker = Thread(target=first_turn)
    worker.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(FileWriteTurnBusyError):
            with file_write_turn(service, "!file write docs:note.md"):
                pass
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert outcomes[0]["ok"] is True
    assert outcomes[1]["error_code"] == "write_limit_reached"
    assert target.read_text(encoding="utf-8") == "one"
