from __future__ import annotations

from pathlib import Path

from leon_agent.file_tools import create_file_tools
from workbench_core.agent import ToolRegistry
from workbench_core.files import FileSearchService
from workbench_core.files.write_service import MAX_WRITE_BYTES, FileWriteService


def _registry(
    root: Path,
    *,
    authorize=lambda request: True,  # noqa: ARG005
) -> tuple[ToolRegistry, FileWriteService]:
    read_service = FileSearchService({"docs": root})
    write_service = FileWriteService({"docs": root}, authorize=authorize)
    return ToolRegistry(create_file_tools(read_service, write_service=write_service)), write_service


def test_write_tools_require_an_authorized_service_with_matching_roots(
    tmp_path: Path,
) -> None:
    read_service = FileSearchService({"docs": tmp_path})

    read_only = ToolRegistry(create_file_tools(read_service))
    no_authorizer = ToolRegistry(
        create_file_tools(
            read_service,
            write_service=FileWriteService({"docs": tmp_path}),
        )
    )
    mismatched = ToolRegistry(
        create_file_tools(
            read_service,
            write_service=FileWriteService(
                {"other": tmp_path},
                authorize=lambda request: True,  # noqa: ARG005
            ),
        )
    )

    for registry in (read_only, no_authorizer, mismatched):
        assert {"create_file", "write_file"}.isdisjoint(registry.names)
        assert {"list_files", "file_search", "read_file"}.issubset(registry.names)


def test_same_root_id_bound_to_different_directories_fails_closed(
    tmp_path: Path,
) -> None:
    read_root = tmp_path / "read"
    write_root = tmp_path / "write"
    read_root.mkdir()
    write_root.mkdir()
    read_service = FileSearchService({"docs": read_root})
    write_service = FileWriteService({"docs": write_root}, authorize=lambda _request: True)

    registry = ToolRegistry(create_file_tools(read_service, write_service=write_service))

    assert {"create_file", "write_file"}.isdisjoint(registry.names)
    assert {"list_files", "file_search", "read_file"}.issubset(registry.names)
    assert str(read_root) not in repr(registry.schemas)
    assert str(write_root) not in repr(registry.schemas)


def test_write_tool_schemas_are_bounded_and_hide_server_control_fields(tmp_path: Path) -> None:
    registry, write_service = _registry(tmp_path)
    schemas = {item["function"]["name"]: item["function"] for item in registry.schemas}

    for name in ("create_file", "write_file"):
        parameters = schemas[name]["parameters"]
        assert parameters["required"] == ["root_id", "relative_path", "content"]
        assert parameters["additionalProperties"] is False
        assert set(parameters["properties"]) == {"root_id", "relative_path", "content"}
        assert parameters["properties"]["root_id"]["enum"] == ["docs"]
        assert parameters["properties"]["relative_path"]["maxLength"] == 500
        assert parameters["properties"]["content"]["maxLength"] == MAX_WRITE_BYTES
        for hidden in ("confirmed", "authorization", "user_message", "writes_used"):
            assert hidden not in parameters["properties"]
    for fingerprint in write_service.root_bindings.values():
        assert fingerprint not in repr(schemas)


def test_create_never_overwrites_and_write_replaces_the_complete_file(tmp_path: Path) -> None:
    registry, service = _registry(tmp_path)
    target = tmp_path / "notes.md"

    created = registry.execute(
        "create_file",
        {"root_id": "docs", "relative_path": "notes.md", "content": "first"},
    )
    assert created == {
        "ok": True,
        "root_id": "docs",
        "path": "notes.md",
        "citation": "docs:notes.md",
        "bytes": 5,
        "created": True,
    }
    assert target.read_text(encoding="utf-8") == "first"
    assert str(tmp_path) not in repr(created)

    service.reset_write_budget()
    duplicate = registry.execute(
        "create_file",
        {"root_id": "docs", "relative_path": "notes.md", "content": "second"},
    )
    assert duplicate["error_code"] == "already_exists"
    assert target.read_text(encoding="utf-8") == "first"

    service.reset_write_budget()
    replaced = registry.execute(
        "write_file",
        {"root_id": "docs", "relative_path": "notes.md", "content": "second"},
    )
    assert replaced == {
        "ok": True,
        "root_id": "docs",
        "path": "notes.md",
        "citation": "docs:notes.md",
        "bytes": 6,
        "overwritten": True,
    }
    assert target.read_text(encoding="utf-8") == "second"

    service.reset_write_budget()
    missing = registry.execute(
        "write_file",
        {"root_id": "docs", "relative_path": "missing.md", "content": "value"},
    )
    assert missing["error_code"] == "not_found"
    assert not (tmp_path / "missing.md").exists()


def test_write_authorization_and_per_turn_limit_are_enforced_server_side(
    tmp_path: Path,
) -> None:
    denied, _ = _registry(tmp_path, authorize=lambda request: False)  # noqa: ARG005
    denied_result = denied.execute(
        "create_file",
        {"root_id": "docs", "relative_path": "denied.md", "content": "value"},
    )
    assert denied_result["error_code"] == "authorization_required"
    assert not (tmp_path / "denied.md").exists()

    registry, _ = _registry(tmp_path)
    first = registry.execute(
        "create_file",
        {"root_id": "docs", "relative_path": "one.md", "content": "one"},
    )
    second = registry.execute(
        "create_file",
        {"root_id": "docs", "relative_path": "two.md", "content": "two"},
    )
    assert first["ok"] is True
    assert second["error_code"] == "write_limit_reached"
    assert (tmp_path / "one.md").is_file()
    assert not (tmp_path / "two.md").exists()


def test_write_handlers_reject_unsafe_paths_and_sensitive_content(tmp_path: Path) -> None:
    registry, service = _registry(tmp_path)
    cases = [
        ("../escape.md", "safe", "path_outside_root"),
        (str(tmp_path / "absolute.md"), "safe", "invalid_path"),
        (".env", "safe", "blocked_path"),
        ("image.png", "safe", "unsupported_file_type"),
        ("private.md", "-----BEGIN PRIVATE KEY-----", "sensitive_content"),
    ]

    for relative_path, content, error_code in cases:
        service.reset_write_budget()
        result = registry.execute(
            "create_file",
            {"root_id": "docs", "relative_path": relative_path, "content": content},
        )
        assert result["error_code"] == error_code
        assert str(tmp_path) not in repr(result)


def test_write_audit_projection_never_contains_content_or_error_text(tmp_path: Path) -> None:
    registry, write_service = _registry(tmp_path)
    marker = "raw-content-must-not-enter-audit"

    arguments = registry.audit_arguments(
        "create_file",
        {
            "root_id": "docs",
            "relative_path": "notes.md",
            "content": marker,
            "confirmed": True,
        },
    )
    assert arguments == {"root_id": "docs", "relative_path": "notes.md"}
    assert marker not in repr(arguments)

    success = registry.audit_result(
        "create_file",
        {
            "ok": True,
            "created": True,
            "root_id": "docs",
            "path": "notes.md",
            "citation": "docs:notes.md",
            "bytes": 5,
            "content": marker,
            "absolute_path": f"{tmp_path}\\notes.md",
        },
    )
    assert success == {
        "ok": True,
        "created": True,
        "root_id": "docs",
        "path": "notes.md",
        "citation": "docs:notes.md",
        "bytes": 5,
    }
    assert marker not in repr(success)
    assert str(tmp_path) not in repr(success)

    failure = registry.audit_result(
        "write_file",
        {
            "ok": False,
            "error_code": "blocked_path",
            "error": marker,
            "content": marker,
        },
    )
    assert failure == {"ok": False, "error_code": "blocked_path"}
    assert registry.audit_result(
        "write_file",
        {"ok": False, "error_code": marker, "error": marker},
    ) == {"ok": False, "error_code": "tool_failed"}
    for fingerprint in write_service.root_bindings.values():
        assert fingerprint not in repr((arguments, success, failure))


def test_write_audit_projection_rejects_absolute_parent_and_forged_locations(
    tmp_path: Path,
) -> None:
    registry, _ = _registry(tmp_path)

    for arguments in (
        {"root_id": "docs", "relative_path": str(tmp_path / "notes.md"), "content": "x"},
        {"root_id": "docs", "relative_path": "../notes.md", "content": "x"},
        {"root_id": "forged", "relative_path": "notes.md", "content": "x"},
    ):
        projected = registry.audit_arguments("create_file", arguments)
        assert projected == {"audit_error": "unsafe_path"}
        assert str(tmp_path) not in repr(projected)

    forged_result = registry.audit_result(
        "create_file",
        {
            "ok": True,
            "created": True,
            "root_id": "docs",
            "path": str(tmp_path / "notes.md"),
            "citation": f"docs:{tmp_path / 'notes.md'}",
            "bytes": 1,
        },
    )
    assert forged_result == {"audit_error": "invalid_result"}
    assert str(tmp_path) not in repr(forged_result)
