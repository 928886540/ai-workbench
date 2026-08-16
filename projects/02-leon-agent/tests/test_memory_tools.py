from __future__ import annotations

from pathlib import Path
from typing import Any

from leon_agent.memory.service import MemoryService
from leon_agent.memory.store import MemoryStore
from leon_agent.memory.tools import create_memory_tools
from workbench_core.agent import ToolRegistry


def _tool_map(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    service = MemoryService(MemoryStore(tmp_path / "memory.db"), session_id="session-1")
    return {tool.name: tool for tool in create_memory_tools(service, **kwargs)}


def test_memory_tools_expose_bounded_schemas_without_context_fields(tmp_path: Path) -> None:
    tools = _tool_map(tmp_path)

    assert set(tools) == {"memory_get", "memory_upsert", "memory_delete"}
    get_parameters = tools["memory_get"].schema["function"]["parameters"]
    upsert_parameters = tools["memory_upsert"].schema["function"]["parameters"]
    delete_parameters = tools["memory_delete"].schema["function"]["parameters"]

    assert get_parameters["properties"]["scope"]["enum"] == [
        "effective",
        "user",
        "global",
    ]
    assert get_parameters["properties"]["prefix"]["pattern"].startswith("^")
    assert get_parameters["properties"]["limit"]["maximum"] == 20
    assert upsert_parameters["required"] == ["key", "value"]
    assert upsert_parameters["properties"]["scope"]["enum"] == ["user", "global"]
    assert upsert_parameters["properties"]["value"]["maxProperties"] == 32
    assert delete_parameters["required"] == ["key"]
    assert "user_message" not in upsert_parameters["properties"]
    assert "writes_used" not in upsert_parameters["properties"]
    assert "*" not in delete_parameters["properties"]["key"].get("enum", [])


def test_memory_get_forwards_filters_and_marks_values_untrusted(tmp_path: Path) -> None:
    tools = _tool_map(tmp_path)
    result = tools["memory_get"].handler(
        key="image.preference",
        scope="effective",
        limit=7,
    )

    assert result == {
        "ok": True,
        "scope": "effective",
        "memories": [],
        "count": 0,
        "untrusted_data": True,
    }


def test_writes_fail_closed_without_user_context_and_preserve_error_code(tmp_path: Path) -> None:
    tools = _tool_map(tmp_path)

    upsert = tools["memory_upsert"].handler(
        key="image.preference",
        value={"style": "cinematic"},
    )
    delete = tools["memory_delete"].handler(key="image.preference")

    assert upsert["error_code"] == "explicit_consent_required"
    assert delete["error_code"] == "explicit_consent_required"
    assert "cinematic" not in str(upsert)


def test_handlers_revalidate_filters_limits_secrets_and_exact_delete_keys(
    tmp_path: Path,
) -> None:
    context = {"message": "记住这个偏好", "writes": 0}
    tools = _tool_map(
        tmp_path,
        user_message_provider=lambda: context["message"],
        writes_used_provider=lambda: context["writes"],
    )

    ambiguous = tools["memory_get"].handler(key="image.one", prefix="image.")
    oversized = tools["memory_get"].handler(limit=21)
    secret = "sk-1234567890abcdef1234"
    rejected = tools["memory_upsert"].handler(
        key="provider.preference",
        value={"value": secret},
    )
    context["message"] = "忘掉这条记忆"
    wildcard = tools["memory_delete"].handler(key="*")

    assert ambiguous["error_code"] == "ambiguous_filter"
    assert oversized["error_code"] == "invalid_limit"
    assert rejected["error_code"] == "sensitive_value_rejected"
    assert secret not in str(rejected)
    assert wildcard["error_code"] == "invalid_key"


def test_write_context_is_server_supplied_and_success_does_not_echo_value(tmp_path: Path) -> None:
    context = {"message": "记住我喜欢电影感照片", "writes": 0}
    tools = _tool_map(
        tmp_path,
        user_message_provider=lambda: context["message"],
        writes_used_provider=lambda: context["writes"],
    )

    upsert = tools["memory_upsert"].handler(
        key="image.preference",
        value={"style": "cinematic"},
    )
    assert upsert["ok"] is True
    assert "cinematic" not in str(upsert)

    context["writes"] = 1
    second = tools["memory_upsert"].handler(
        key="image.preference",
        value={"style": "noir"},
    )
    assert second["error_code"] == "write_limit_reached"


def test_delete_forwards_scope_and_reports_global_fallback(tmp_path: Path) -> None:
    context = {"message": "全局记住我的默认语言是中文", "writes": 0}
    tools = _tool_map(
        tmp_path,
        user_message_provider=lambda: context["message"],
        writes_used_provider=lambda: context["writes"],
    )
    global_result = tools["memory_upsert"].handler(
        key="profile.language",
        value={"value": "zh-CN"},
        scope="global",
    )
    assert global_result["ok"] is True

    context["message"] = "记住我的当前语言"
    context["writes"] = 0
    user_result = tools["memory_upsert"].handler(
        key="profile.language",
        value={"value": "en-US"},
    )
    assert user_result["ok"] is True

    context["message"] = "忘掉我的当前语言"
    deleted = tools["memory_delete"].handler(key="profile.language")
    assert deleted == {
        "ok": True,
        "scope": "user",
        "key": "profile.language",
        "deleted": True,
        "global_fallback": True,
    }


def test_audit_projections_drop_values_unknown_fields_and_error_text(tmp_path: Path) -> None:
    service = MemoryService(MemoryStore(tmp_path / "memory.db"), session_id="session-1")
    registry = ToolRegistry(create_memory_tools(service))
    secret = "must-not-enter-audit"

    assert registry.audit_arguments(
        "memory_upsert",
        {
            "scope": "user",
            "key": "image.preference",
            "value": {"style": secret},
            "unknown": secret,
        },
    ) == {"scope": "user", "key": "image.preference"}
    assert registry.audit_result(
        "memory_upsert",
        {
            "ok": True,
            "created": True,
            "updated_at": 123,
            "value": {"style": secret},
            "source_session_id": secret,
        },
    ) == {"ok": True, "created": True, "updated_at": 123}

    projected_get = registry.audit_result(
        "memory_get",
        {
            "ok": True,
            "scope": "effective",
            "count": 1,
            "memories": [
                {
                    "scope": "user",
                    "key": "image.preference",
                    "value": {"style": secret},
                    "source_session_id": secret,
                }
            ],
            "unknown": secret,
        },
    )
    assert projected_get == {
        "scope": "effective",
        "count": 1,
        "memories": [{"scope": "user", "key": "image.preference"}],
    }
    assert secret not in repr(projected_get)

    assert registry.audit_result(
        "memory_delete",
        {
            "ok": True,
            "scope": "user",
            "key": "image.preference",
            "deleted": True,
            "global_fallback": False,
            "value": secret,
        },
    ) == {
        "scope": "user",
        "key": "image.preference",
        "deleted": True,
        "global_fallback": False,
    }
    assert registry.audit_result(
        "memory_upsert",
        {
            "ok": False,
            "error_code": "sensitive_value_rejected",
            "error": secret,
            "value": secret,
        },
    ) == {"ok": False, "error_code": "sensitive_value_rejected"}
    assert registry.audit_result(
        "memory_get",
        {"ok": False, "error": secret, "unknown": secret},
    ) == {"ok": False, "error_code": "tool_failed"}
    assert registry.audit_result(
        "memory_delete",
        {"ok": False, "error_code": secret, "error": secret},
    ) == {"ok": False, "error_code": "tool_failed"}
