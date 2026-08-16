from __future__ import annotations

import json
from pathlib import Path

import pytest
from leon_agent.memory.service import (
    MemoryPolicyError,
    MemoryService,
    authorize_memory_write,
    contains_sensitive_key,
    contains_sensitive_value,
    detect_memory_intent,
)
from leon_agent.memory.store import SQLITE_INTEGER_MAX, MemoryStore


def test_intent_gate_defaults_to_no_write_for_ordinary_statements() -> None:
    statement = detect_memory_intent("我喜欢电影感照片")
    assert statement.operation == "none"
    assert statement.explicit is False
    ordinary = detect_memory_intent("帮我解释一下 Agent loop")
    assert ordinary.operation == "none"
    assert ordinary.explicit is False

    save = detect_memory_intent("请记住我喜欢电影感照片")
    assert save.operation == "upsert"
    assert save.explicit is True
    assert save.global_requested is False

    delete = detect_memory_intent("请忘掉 image.preference 这条记忆")
    assert delete.operation == "delete"
    assert delete.explicit is True

    global_save = detect_memory_intent("全局记住我的默认语言是中文")
    assert global_save.operation == "upsert"
    assert global_save.global_requested is True

    default_save = detect_memory_intent("以后默认用电影感照片")
    assert default_save.operation == "upsert"
    preference_save = detect_memory_intent("保存为偏好：电影感")
    assert preference_save.operation == "upsert"

    assert detect_memory_intent("不要记住我的密码").operation == "none"
    assert detect_memory_intent("don't save this").operation == "none"
    assert detect_memory_intent("我不想记住我的偏好").operation == "none"
    assert detect_memory_intent("我不希望你记住这个").operation == "none"
    assert detect_memory_intent("我不想让你记住这个").operation == "none"
    assert detect_memory_intent("我不希望 Leon 记住这个").operation == "none"
    assert detect_memory_intent("I do not want you to remember this").operation == "none"
    assert detect_memory_intent("I don't want Leon to remember this").operation == "none"


@pytest.mark.parametrize(
    ("message", "operation", "scope", "code"),
    [
        ("我喜欢电影感照片", "upsert", "user", "explicit_consent_required"),
        ("请记住这个偏好", "delete", "user", "explicit_consent_required"),
        ("请记住这个偏好", "upsert", "global", "global_scope_requires_explicit"),
        ("请记住并忘掉这个偏好", "upsert", "user", "ambiguous_intent"),
        ("不要记住这个偏好", "upsert", "user", "explicit_consent_required"),
    ],
)
def test_authorize_memory_write_returns_stable_codes(
    message: str,
    operation: str,
    scope: str,
    code: str,
) -> None:
    with pytest.raises(MemoryPolicyError) as raised:
        authorize_memory_write(message, operation, scope=scope)
    assert raised.value.code == code
    assert "这个偏好" not in raised.value.message

    with pytest.raises(MemoryPolicyError) as raised:
        authorize_memory_write("请记住这个偏好", "upsert", writes_used=1)
    assert raised.value.code == "write_limit_reached"


def test_service_writes_source_and_time_but_does_not_echo_value(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store, session_id="session-123", clock=lambda: 1234)

    result = service.upsert(
        "image.preference",
        {"style": "cinematic"},
        user_message="记住我喜欢电影感照片",
    )
    assert result == {
        "ok": True,
        "created": True,
        "scope": "user",
        "key": "image.preference",
        "source_kind": "explicit_user_request",
        "source_session_id": "session-123",
        "updated_at": 1234,
    }

    read = service.get(key="image.preference")
    assert read["ok"] is True
    assert read["memories"][0]["value"] == {"style": "cinematic"}
    assert read["memories"][0]["source_session_id"] == "session-123"
    assert read["untrusted_data"] is True


def test_service_rejects_ordinary_chat_and_sensitive_values_without_persisting(
    tmp_path: Path,
) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store, session_id="session-123")

    ordinary = service.upsert(
        "image.preference",
        {"style": "cinematic"},
        user_message="我喜欢电影感照片",
    )
    assert ordinary["ok"] is False
    assert ordinary["error_code"] == "explicit_consent_required"
    assert store.count("user", "local-owner") == 0

    secret = "sk-1234567890abcdef1234"
    rejected = service.upsert(
        "provider.api_key",
        {"value": secret},
        user_message="记住这个 provider 配置",
    )
    assert rejected == {
        "ok": False,
        "error_code": "sensitive_value_rejected",
        "error": "memory value rejected by privacy policy.",
    }
    assert secret not in json.dumps(rejected, ensure_ascii=False)
    assert store.count("user", "local-owner") == 0
    key_rejected = service.upsert(
        "api_token",
        {"value": "not-a-real-token"},
        user_message="记住这个字段",
    )
    assert key_rejected["error_code"] == "sensitive_value_rejected"
    assert contains_sensitive_value({"nested": {"note": "Bearer abcdefghijklmnop"}}) is True
    assert contains_sensitive_value({"style": "cinematic"}) is False
    assert contains_sensitive_key("provider.api_token") is True
    assert contains_sensitive_key("image.preference") is False


def test_global_scope_requires_global_language_and_delete_reports_fallback(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(store, session_id="session-123", clock=lambda: 100)

    denied = service.upsert(
        "profile.language",
        {"value": "zh-CN"},
        scope="global",
        user_message="记住我的语言",
    )
    assert denied["error_code"] == "global_scope_requires_explicit"

    global_result = service.upsert(
        "profile.language",
        {"value": "zh-CN"},
        scope="global",
        user_message="全局记住我的语言",
    )
    assert global_result["ok"] is True
    user_result = service.upsert(
        "profile.language",
        {"value": "en-US"},
        user_message="记住我的当前语言",
    )
    assert user_result["ok"] is True

    deleted = service.delete(
        "profile.language",
        user_message="忘掉我的当前语言",
    )
    assert deleted == {
        "ok": True,
        "scope": "user",
        "key": "profile.language",
        "deleted": True,
        "global_fallback": True,
    }
    effective = service.get(key="profile.language")
    assert effective["memories"][0]["value"] == {"value": "zh-CN"}


def test_service_get_filters_and_errors_are_safe(tmp_path: Path) -> None:
    service = MemoryService(MemoryStore(tmp_path / "memory.db"), session_id="session-1")
    service.upsert("image.one", {"v": 1}, user_message="记住第一条")
    service.upsert("image.two", {"v": 2}, user_message="记住第二条")

    listed = service.get(prefix="image_", limit=2)
    assert listed["ok"] is True
    assert listed["count"] == 0
    listed = service.get(prefix="image.", limit=2)
    assert [item["key"] for item in listed["memories"]] == ["image.two", "image.one"]

    invalid = service.get(scope="nope")
    assert invalid == {
        "ok": False,
        "error_code": "invalid_scope",
        "error": "scope must be user, global, or effective.",
    }
    too_many = service.get(limit=21)
    assert too_many["error_code"] == "invalid_limit"


def test_service_enforces_fixed_principal_and_delete_consent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    with pytest.raises(MemoryPolicyError) as raised:
        MemoryService(store, session_id="session-1", principal="other-user")
    assert raised.value.code == "invalid_principal"

    service = MemoryService(store, session_id="session-1")
    service.upsert("image.preference", {"style": "cinematic"}, user_message="记住这个偏好")
    denied = service.delete("image.preference", user_message="删掉一个文件")
    assert denied["error_code"] == "explicit_consent_required"
    assert service.get(key="image.preference")["count"] == 1


def test_service_rejects_clock_values_outside_sqlite_integer_range(tmp_path: Path) -> None:
    service = MemoryService(
        MemoryStore(tmp_path / "memory.db"),
        session_id="session-1",
        clock=lambda: SQLITE_INTEGER_MAX + 1,
    )
    result = service.upsert(
        "image.preference",
        {"style": "cinematic"},
        user_message="记住这个偏好",
    )
    assert result == {
        "ok": False,
        "error_code": "invalid_timestamp",
        "error": "memory clock returned an invalid value.",
    }
