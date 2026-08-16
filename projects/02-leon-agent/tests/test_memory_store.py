from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import leon_agent.memory.store as store_module
import pytest
from leon_agent.memory.store import (
    GLOBAL_OWNER,
    GLOBAL_SCOPE,
    SQLITE_INTEGER_MAX,
    USER_SCOPE,
    MemoryStore,
    MemoryStoreError,
)


def _upsert(
    store: MemoryStore,
    *,
    scope: str = USER_SCOPE,
    owner: str = "local-owner",
    key: str = "image.preference",
    value: dict | None = None,
    now_ms: int = 100,
):
    return store.upsert(
        scope,
        owner,
        key,
        {"style": "cinematic"} if value is None else value,
        source_kind="explicit_user_request",
        source_session_id="session-1",
        now_ms=now_ms,
    )


def test_store_creates_only_memory_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path)

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert tables == {"memories"}
    assert {"idx_memories_owner_updated", "idx_memories_owner_key"} <= indexes
    assert store.count(USER_SCOPE, "local-owner") == 0


def test_upsert_reopen_and_same_timestamp_keep_created_flag(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path)

    first = _upsert(store, now_ms=100)
    assert first.created is True
    assert first.record.created_at == 100
    assert first.record.updated_at == 100

    second = _upsert(store, now_ms=100, value={"style": "noir"})
    assert second.created is False
    assert second.record.created_at == 100
    assert second.record.value == {"style": "noir"}

    reopened = MemoryStore(db_path)
    record = reopened.get(USER_SCOPE, "local-owner", "image.preference")
    assert record is not None
    assert record.value == {"style": "noir"}
    assert record.source_session_id == "session-1"


def test_user_overrides_global_and_delete_reveals_fallback(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    _upsert(
        store,
        scope=GLOBAL_SCOPE,
        owner="ignored-owner",
        value={"style": "global"},
        now_ms=100,
    )
    _upsert(store, value={"style": "user"}, now_ms=200)

    effective = store.list_effective("local-owner")
    assert len(effective) == 1
    assert effective[0].scope == USER_SCOPE
    assert effective[0].value == {"style": "user"}
    assert store.get(GLOBAL_SCOPE, GLOBAL_OWNER, "image.preference") is not None

    assert store.delete(USER_SCOPE, "local-owner", "image.preference") is True
    fallback = store.list_effective("local-owner")
    assert len(fallback) == 1
    assert fallback[0].scope == GLOBAL_SCOPE
    assert fallback[0].value == {"style": "global"}
    assert store.delete(USER_SCOPE, "local-owner", "image.preference") is False


def test_prefix_is_literal_and_effective_limit_applies_after_dedup(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    _upsert(store, key="foo_bar", now_ms=100)
    _upsert(store, key="foo_baz", now_ms=90)
    _upsert(store, key="fooXbar", now_ms=80)

    matches = store.list(USER_SCOPE, "local-owner", prefix="foo_")
    assert [item.key for item in matches] == ["foo_bar", "foo_baz"]
    with pytest.raises(MemoryStoreError) as raised:
        store.list(USER_SCOPE, "local-owner", prefix="foo%")
    assert raised.value.code == "invalid_prefix"

    _upsert(
        store,
        scope=GLOBAL_SCOPE,
        key="shared.one",
        value={"v": "global"},
        now_ms=200,
    )
    _upsert(
        store,
        key="shared.one",
        value={"v": "user"},
        now_ms=190,
    )
    effective = store.list_effective("local-owner", limit=2)
    assert len(effective) == 2
    assert len({item.key for item in effective}) == 2
    assert next(item for item in effective if item.key == "shared.one").value == {"v": "user"}


@pytest.mark.parametrize(
    ("key", "value", "code"),
    [
        ("bad key", {"x": 1}, "invalid_key"),
        ("x" * 81, {"x": 1}, "invalid_key"),
        ("valid.key", [], "invalid_value"),
        ("valid.key", {"x": "\x00"}, "control_character"),
        ("valid.key", {"x": "\ud800"}, "invalid_unicode"),
        ("valid.key", {"\ud800": "x"}, "invalid_unicode"),
        ("valid.key", {"x": "y" * 4_100}, "value_too_large"),
        ("valid.key", {str(index): index for index in range(33)}, "value_fields_exceeded"),
        ("valid.key", {"x": float("nan")}, "invalid_value"),
    ],
)
def test_store_rejects_invalid_values(
    tmp_path: Path,
    key: str,
    value: object,
    code: str,
) -> None:
    store = MemoryStore(tmp_path / f"{code}.db")
    with pytest.raises(MemoryStoreError) as raised:
        _upsert(store, key=key, value=value)  # type: ignore[arg-type]
    assert raised.value.code == code


def test_store_rejects_excessive_nesting_and_invalid_metadata(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    deep = {"a": {"b": {"c": {"d": {"e": 1}}}}}
    with pytest.raises(MemoryStoreError) as raised:
        _upsert(store, value=deep)
    assert raised.value.code == "value_depth_exceeded"

    with pytest.raises(MemoryStoreError) as raised:
        store.upsert(
            USER_SCOPE,
            "local-owner",
            "valid.key",
            {"x": 1},
            source_kind="bad\nsource",
            source_session_id="session-1",
        )
    assert raised.value.code == "control_character"

    with pytest.raises(MemoryStoreError) as raised:
        _upsert(store, value={"x": "\x85"})
    assert raised.value.code == "control_character"

    with pytest.raises(MemoryStoreError) as raised:
        _upsert(store, key="bad\ud800")
    assert raised.value.code == "invalid_unicode"

    with pytest.raises(MemoryStoreError) as raised:
        store.list(USER_SCOPE, "local-owner", prefix="bad\ud800")
    assert raised.value.code == "invalid_unicode"

    with pytest.raises(MemoryStoreError) as raised:
        store.upsert(
            USER_SCOPE,
            "owner\ud800",
            "valid.key",
            {"x": 1},
            source_kind="explicit_user_request",
            source_session_id="session-1",
        )
    assert raised.value.code == "invalid_unicode"

    with pytest.raises(MemoryStoreError) as raised:
        store.upsert(
            USER_SCOPE,
            "local-owner",
            "valid.key",
            {"x": 1},
            source_kind="source\ud800",
            source_session_id="session-1",
        )
    assert raised.value.code == "invalid_unicode"

    with pytest.raises(MemoryStoreError) as raised:
        store.upsert(
            USER_SCOPE,
            "local-owner",
            "valid.key",
            {"x": 1},
            source_kind="explicit_user_request",
            source_session_id="session\ud800",
        )
    assert raised.value.code == "invalid_unicode"


def test_timestamp_stays_inside_sqlite_integer_range(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    boundary = _upsert(store, now_ms=SQLITE_INTEGER_MAX)
    assert boundary.record.updated_at == SQLITE_INTEGER_MAX

    with pytest.raises(MemoryStoreError) as raised:
        _upsert(store, key="too-large", now_ms=SQLITE_INTEGER_MAX + 1)
    assert raised.value.code == "invalid_timestamp"


def test_corrupt_timestamp_returns_stable_storage_error(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path)
    _upsert(store)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE memories SET created_at = ?, updated_at = ?",
            ("not-int", "not-int"),
        )

    with pytest.raises(MemoryStoreError) as raised:
        store.get(USER_SCOPE, "local-owner", "image.preference")
    assert raised.value.code == "storage_corrupt"


def test_capacity_is_checked_inside_write_transaction(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store_module, "MAX_USER_MEMORIES", 1)
    store = MemoryStore(tmp_path / "memory.db")
    _upsert(store, key="one", now_ms=1)
    with pytest.raises(MemoryStoreError) as raised:
        _upsert(store, key="two", now_ms=2)
    assert raised.value.code == "memory_limit_reached"
    assert store.count(USER_SCOPE, "local-owner") == 1


def test_busy_timeout_is_configured_without_enabling_wal(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db", busy_timeout_ms=1234)
    connection = store._connect()
    try:
        timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert timeout == 1234
    assert str(journal_mode).casefold() == "delete"


def test_busy_write_returns_stable_error_after_finite_retries(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db", busy_timeout_ms=1, sleep=lambda _: None)
    lock = store._connect()
    lock.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(MemoryStoreError) as raised:
            _upsert(store, key="locked", now_ms=1)
        assert raised.value.code == "storage_busy"
    finally:
        lock.rollback()
        lock.close()


def test_concurrent_writes_remain_valid(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")

    def write(index: int) -> None:
        _upsert(store, key=f"parallel.{index}", now_ms=index + 1)

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(write, range(12)))

    assert store.count(USER_SCOPE, "local-owner") == 12
    assert len(store.list(USER_SCOPE, "local-owner", limit=20)) == 12


def test_stored_json_is_canonical_and_session_tables_are_untouched(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path)
    _upsert(store, value={"z": 1, "a": "two"})
    with sqlite3.connect(db_path) as connection:
        raw = connection.execute("SELECT value_json FROM memories").fetchone()[0]
    assert raw == json.dumps({"a": "two", "z": 1}, ensure_ascii=False, separators=(",", ":"))
    assert "sessions" not in {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
