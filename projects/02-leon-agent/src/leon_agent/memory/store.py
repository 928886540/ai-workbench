"""SQLite persistence for explicit, cross-session Leon memories.

The store deliberately owns only the ``memories`` table. Conversation and tool
history remain the responsibility of :mod:`leon_agent.session`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PRINCIPAL = "local-owner"
USER_SCOPE = "user"
GLOBAL_SCOPE = "global"
EFFECTIVE_SCOPE = "effective"
GLOBAL_OWNER = "*"

MAX_KEY_LENGTH = 80
MAX_VALUE_BYTES = 4 * 1024
MAX_VALUE_DEPTH = 4
MAX_VALUE_FIELDS = 32
MAX_USER_MEMORIES = 200
MAX_GLOBAL_MEMORIES = 100
MAX_LIST_LIMIT = max(MAX_USER_MEMORIES, MAX_GLOBAL_MEMORIES)
SQLITE_BUSY_RETRIES = 3
SQLITE_BUSY_SLEEP_SECONDS = 0.02
SQLITE_INTEGER_MIN = 0
SQLITE_INTEGER_MAX = 2**63 - 1

_KEY_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,78}[a-z0-9])?$")
_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in value)


def _has_invalid_unicode(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


class MemoryStoreError(ValueError):
    """A safe, stable error returned by the memory persistence boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class MemoryRecord:
    """Internal representation of one persisted memory."""

    scope: str
    owner_id: str
    key: str
    value: dict[str, Any]
    source_kind: str
    source_session_id: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class MemoryWriteResult:
    """Persisted record plus whether the operation inserted a new key."""

    record: MemoryRecord
    created: bool


def normalize_key(value: Any) -> str:
    """Normalize and validate a model-facing memory key."""

    if not isinstance(value, str):
        raise MemoryStoreError("invalid_key", "key must be a string.")
    if _has_invalid_unicode(value):
        raise MemoryStoreError("invalid_unicode", "key contains invalid Unicode.")
    normalized = value.strip().casefold()
    if not normalized:
        raise MemoryStoreError("invalid_key", "key must not be empty.")
    if len(normalized) > MAX_KEY_LENGTH or not _KEY_PATTERN.fullmatch(normalized):
        raise MemoryStoreError(
            "invalid_key",
            "key must use lowercase letters, digits, '.', '_' or '-' and be at most 80 characters.",
        )
    return normalized


def normalize_prefix(value: Any) -> str:
    """Normalize a literal key prefix; punctuation may appear at the end."""

    if not isinstance(value, str):
        raise MemoryStoreError("invalid_prefix", "prefix must be a string.")
    if _has_invalid_unicode(value):
        raise MemoryStoreError("invalid_unicode", "prefix contains invalid Unicode.")
    normalized = value.strip().casefold()
    if (
        not normalized
        or len(normalized) > MAX_KEY_LENGTH
        or not _PREFIX_PATTERN.fullmatch(normalized)
    ):
        raise MemoryStoreError("invalid_prefix", "prefix is invalid.")
    return normalized


def normalize_scope(value: Any, *, allow_effective: bool = False) -> str:
    """Normalize a scope without accepting arbitrary model-controlled values."""

    if not isinstance(value, str):
        raise MemoryStoreError("invalid_scope", "scope must be a string.")
    if _has_invalid_unicode(value):
        raise MemoryStoreError("invalid_unicode", "scope contains invalid Unicode.")
    normalized = value.strip().casefold()
    allowed = {USER_SCOPE, GLOBAL_SCOPE}
    if allow_effective:
        allowed.add(EFFECTIVE_SCOPE)
    if normalized not in allowed:
        choices = "user, global, or effective" if allow_effective else "user or global"
        raise MemoryStoreError("invalid_scope", f"scope must be {choices}.")
    return normalized


def normalize_owner_id(value: Any, *, scope: str) -> str:
    """Normalize the storage owner; global rows always use the sentinel owner."""

    if not isinstance(value, str):
        raise MemoryStoreError("invalid_principal", "owner_id must be a string.")
    if _has_invalid_unicode(value):
        raise MemoryStoreError("invalid_unicode", "owner_id contains invalid Unicode.")
    if scope == GLOBAL_SCOPE:
        return GLOBAL_OWNER
    normalized = value.strip()
    if (
        not normalized
        or normalized == GLOBAL_OWNER
        or len(normalized) > 128
        or _has_control_character(normalized)
        or _has_invalid_unicode(normalized)
    ):
        raise MemoryStoreError("invalid_principal", "owner_id is invalid.")
    return normalized


def _validate_value_node(value: Any, *, depth: int, field_count: list[int]) -> None:
    if depth > MAX_VALUE_DEPTH:
        raise MemoryStoreError("value_depth_exceeded", "value nesting is too deep.")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise MemoryStoreError(
                    "invalid_value",
                    "value object keys must be non-empty strings.",
                )
            if _has_invalid_unicode(key):
                raise MemoryStoreError("invalid_unicode", "value contains invalid Unicode.")
            if _has_control_character(key):
                raise MemoryStoreError("control_character", "value contains a control character.")
            field_count[0] += 1
            if field_count[0] > MAX_VALUE_FIELDS:
                raise MemoryStoreError("value_fields_exceeded", "value contains too many fields.")
            _validate_value_node(child, depth=depth + 1, field_count=field_count)
        return
    if isinstance(value, list):
        if len(value) > MAX_VALUE_FIELDS:
            raise MemoryStoreError("value_fields_exceeded", "value contains too many items.")
        for child in value:
            _validate_value_node(child, depth=depth + 1, field_count=field_count)
        return
    if isinstance(value, str):
        if _has_control_character(value):
            raise MemoryStoreError("control_character", "value contains a control character.")
        if _has_invalid_unicode(value):
            raise MemoryStoreError("invalid_unicode", "value contains invalid Unicode.")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise MemoryStoreError("invalid_value", "value must contain JSON-compatible data.")


def normalize_value(value: Any) -> dict[str, Any]:
    """Validate a structured JSON object and return a detached canonical copy."""

    if not isinstance(value, Mapping) or isinstance(value, (str, bytes, list)):
        raise MemoryStoreError("invalid_value", "value must be a JSON object.")
    candidate = dict(value)
    _validate_value_node(candidate, depth=1, field_count=[0])
    try:
        encoded = json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        detached = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MemoryStoreError("invalid_value", "value must be valid JSON.") from exc
    try:
        encoded_bytes = len(encoded.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise MemoryStoreError("invalid_unicode", "value contains invalid Unicode.") from exc
    if encoded_bytes > MAX_VALUE_BYTES:
        raise MemoryStoreError("value_too_large", "value exceeds the 4 KiB limit.")
    if not isinstance(detached, dict):  # pragma: no cover - guarded by the input check
        raise MemoryStoreError("invalid_value", "value must be a JSON object.")
    return detached


def _safe_metadata(value: Any, *, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise MemoryStoreError("invalid_metadata", f"{name} must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise MemoryStoreError("invalid_metadata", f"{name} is invalid.")
    if _has_invalid_unicode(normalized):
        raise MemoryStoreError("invalid_unicode", f"{name} contains invalid Unicode.")
    if _has_control_character(normalized):
        raise MemoryStoreError("control_character", f"{name} contains a control character.")
    return normalized


def normalize_timestamp(value: Any, *, name: str = "now_ms") -> int:
    """Validate a timestamp before SQLite integer binding can overflow."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < SQLITE_INTEGER_MIN
        or value > SQLITE_INTEGER_MAX
    ):
        raise MemoryStoreError(
            "invalid_timestamp",
            f"{name} must be an integer between {SQLITE_INTEGER_MIN} and {SQLITE_INTEGER_MAX}.",
        )
    return value


def _now_ms() -> int:
    try:
        timestamp = int(time.time() * 1000)
    except (OverflowError, ValueError) as exc:
        raise MemoryStoreError(
            "invalid_timestamp", "system clock returned an invalid value."
        ) from exc
    return normalize_timestamp(timestamp)


class MemoryStore:
    """Persist explicit memories without touching session or message tables."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self._busy_timeout_ms = busy_timeout_ms
        self._sleep = sleep
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self._busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection

    def _initialize(self) -> None:
        statement = """
            CREATE TABLE IF NOT EXISTS memories (
                scope TEXT NOT NULL CHECK (scope IN ('user', 'global')),
                owner_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_session_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (scope, owner_id, key)
            )
        """
        index_statements = (
            "CREATE INDEX IF NOT EXISTS idx_memories_owner_updated "
            "ON memories (owner_id, scope, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_memories_owner_key "
            "ON memories (owner_id, key)",
        )
        self._run_write(
            lambda connection: self._create_schema(connection, statement, index_statements)
        )

    @staticmethod
    def _create_schema(
        connection: sqlite3.Connection,
        statement: str,
        index_statements: tuple[str, ...],
    ) -> None:
        connection.execute(statement)
        for index_statement in index_statements:
            connection.execute(index_statement)

    def _run_write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(SQLITE_BUSY_RETRIES):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = operation(connection)
                connection.commit()
                return result
            except sqlite3.OperationalError as exc:
                connection.rollback()
                if "locked" not in str(exc).casefold() and "busy" not in str(exc).casefold():
                    raise
                last_error = exc
            finally:
                connection.close()
            if attempt + 1 < SQLITE_BUSY_RETRIES:
                self._sleep(SQLITE_BUSY_SLEEP_SECONDS * (attempt + 1))
        raise MemoryStoreError("storage_busy", "memory storage is busy; try again") from last_error

    def _run_read(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        connection = self._connect()
        try:
            return operation(connection)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                raise MemoryStoreError("storage_busy", "memory storage is busy; try again") from exc
            raise
        finally:
            connection.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        try:
            value = json.loads(str(row["value_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MemoryStoreError("storage_corrupt", "stored memory value is invalid") from exc
        if not isinstance(value, dict):
            raise MemoryStoreError("storage_corrupt", "stored memory value is invalid")
        try:
            created_at = normalize_timestamp(row["created_at"], name="created_at")
            updated_at = normalize_timestamp(row["updated_at"], name="updated_at")
        except (MemoryStoreError, TypeError, ValueError, OverflowError) as exc:
            raise MemoryStoreError("storage_corrupt", "stored memory timestamp is invalid") from exc
        return MemoryRecord(
            scope=str(row["scope"]),
            owner_id=str(row["owner_id"]),
            key=str(row["key"]),
            value=value,
            source_kind=str(row["source_kind"]),
            source_session_id=str(row["source_session_id"]),
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def _limit(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise MemoryStoreError("invalid_limit", "limit must be an integer.")
        if not 1 <= value <= MAX_LIST_LIMIT:
            raise MemoryStoreError("invalid_limit", "limit is outside the allowed range.")
        return value

    def _scope_owner(self, scope: Any, owner_id: Any) -> tuple[str, str]:
        normalized_scope = normalize_scope(scope)
        return normalized_scope, normalize_owner_id(owner_id, scope=normalized_scope)

    @staticmethod
    def _escape_like_prefix(prefix: str) -> str:
        return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def upsert(
        self,
        scope: str,
        owner_id: str,
        key: str,
        value: Mapping[str, Any],
        *,
        source_kind: str,
        source_session_id: str,
        now_ms: int | None = None,
    ) -> MemoryWriteResult:
        normalized_scope, normalized_owner = self._scope_owner(scope, owner_id)
        normalized_key = normalize_key(key)
        normalized_value = normalize_value(value)
        safe_source = _safe_metadata(source_kind, name="source_kind", maximum=64)
        safe_session = _safe_metadata(source_session_id, name="source_session_id", maximum=128)
        timestamp = normalize_timestamp(_now_ms() if now_ms is None else now_ms)

        def operation(connection: sqlite3.Connection) -> MemoryWriteResult:
            existing = connection.execute(
                "SELECT created_at FROM memories WHERE scope = ? AND owner_id = ? AND key = ?",
                (normalized_scope, normalized_owner, normalized_key),
            ).fetchone()
            created = existing is None
            if created:
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM memories WHERE scope = ? AND owner_id = ?",
                        (normalized_scope, normalized_owner),
                    ).fetchone()[0]
                )
                maximum = (
                    MAX_GLOBAL_MEMORIES if normalized_scope == GLOBAL_SCOPE else MAX_USER_MEMORIES
                )
                if count >= maximum:
                    raise MemoryStoreError("memory_limit_reached", "memory capacity is full.")
                created_at = timestamp
                connection.execute(
                    """
                    INSERT INTO memories
                        (scope, owner_id, key, value_json, source_kind, source_session_id,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_scope,
                        normalized_owner,
                        normalized_key,
                        json.dumps(
                            normalized_value,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        safe_source,
                        safe_session,
                        created_at,
                        timestamp,
                    ),
                )
            else:
                created_at = int(existing["created_at"])
                connection.execute(
                    """
                    UPDATE memories
                    SET value_json = ?, source_kind = ?, source_session_id = ?, updated_at = ?
                    WHERE scope = ? AND owner_id = ? AND key = ?
                    """,
                    (
                        json.dumps(
                            normalized_value,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        safe_source,
                        safe_session,
                        timestamp,
                        normalized_scope,
                        normalized_owner,
                        normalized_key,
                    ),
                )
            row = connection.execute(
                """
                SELECT scope, owner_id, key, value_json, source_kind, source_session_id,
                       created_at, updated_at
                FROM memories WHERE scope = ? AND owner_id = ? AND key = ?
                """,
                (normalized_scope, normalized_owner, normalized_key),
            ).fetchone()
            if row is None:  # pragma: no cover - the transaction just wrote the row
                raise MemoryStoreError("storage_error", "memory write did not persist.")
            return MemoryWriteResult(self._row_to_record(row), created)

        return self._run_write(operation)

    def get(self, scope: str, owner_id: str, key: str) -> MemoryRecord | None:
        normalized_scope, normalized_owner = self._scope_owner(scope, owner_id)
        normalized_key = normalize_key(key)

        def operation(connection: sqlite3.Connection) -> MemoryRecord | None:
            row = connection.execute(
                """
                SELECT scope, owner_id, key, value_json, source_kind, source_session_id,
                       created_at, updated_at
                FROM memories WHERE scope = ? AND owner_id = ? AND key = ?
                """,
                (normalized_scope, normalized_owner, normalized_key),
            ).fetchone()
            return self._row_to_record(row) if row is not None else None

        return self._run_read(operation)

    def list(
        self,
        scope: str,
        owner_id: str,
        *,
        key: str | None = None,
        prefix: str | None = None,
        limit: int = MAX_LIST_LIMIT,
    ) -> list[MemoryRecord]:
        normalized_scope, normalized_owner = self._scope_owner(scope, owner_id)
        normalized_key = normalize_key(key) if key is not None else None
        normalized_prefix = normalize_prefix(prefix) if prefix is not None else None
        if normalized_key is not None and normalized_prefix is not None:
            raise MemoryStoreError("ambiguous_filter", "key and prefix cannot both be set.")
        normalized_limit = self._limit(limit)

        def operation(connection: sqlite3.Connection) -> list[MemoryRecord]:
            clauses = ["scope = ?", "owner_id = ?"]
            params: list[Any] = [normalized_scope, normalized_owner]
            if normalized_key is not None:
                clauses.append("key = ?")
                params.append(normalized_key)
            elif normalized_prefix is not None:
                clauses.append("key LIKE ? ESCAPE '\\'")
                params.append(f"{self._escape_like_prefix(normalized_prefix)}%")
            params.append(normalized_limit)
            rows = connection.execute(
                f"""
                SELECT scope, owner_id, key, value_json, source_kind, source_session_id,
                       created_at, updated_at
                FROM memories
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, key ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

        return self._run_read(operation)

    def list_effective(
        self,
        owner_id: str,
        *,
        key: str | None = None,
        prefix: str | None = None,
        limit: int = MAX_LIST_LIMIT,
    ) -> list[MemoryRecord]:
        normalized_owner = normalize_owner_id(owner_id, scope=USER_SCOPE)
        normalized_limit = self._limit(limit)
        # Fetch before limiting so a user override is not lost behind a global row.
        global_rows = self.list(
            GLOBAL_SCOPE,
            GLOBAL_OWNER,
            key=key,
            prefix=prefix,
            limit=MAX_LIST_LIMIT,
        )
        user_rows = self.list(
            USER_SCOPE,
            normalized_owner,
            key=key,
            prefix=prefix,
            limit=MAX_LIST_LIMIT,
        )
        merged = {row.key: row for row in global_rows}
        merged.update({row.key: row for row in user_rows})
        return sorted(
            merged.values(), key=lambda row: (-row.updated_at, row.key)
        )[:normalized_limit]

    def delete(self, scope: str, owner_id: str, key: str) -> bool:
        normalized_scope, normalized_owner = self._scope_owner(scope, owner_id)
        normalized_key = normalize_key(key)

        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "DELETE FROM memories WHERE scope = ? AND owner_id = ? AND key = ?",
                (normalized_scope, normalized_owner, normalized_key),
            )
            return cursor.rowcount > 0

        return bool(self._run_write(operation))

    def delete_with_fallback(self, scope: str, owner_id: str, key: str) -> tuple[bool, bool]:
        """Delete one row and inspect a global fallback in one write transaction."""

        normalized_scope, normalized_owner = self._scope_owner(scope, owner_id)
        normalized_key = normalize_key(key)

        def operation(connection: sqlite3.Connection) -> tuple[bool, bool]:
            cursor = connection.execute(
                "DELETE FROM memories WHERE scope = ? AND owner_id = ? AND key = ?",
                (normalized_scope, normalized_owner, normalized_key),
            )
            fallback = False
            if normalized_scope == USER_SCOPE:
                fallback = (
                    connection.execute(
                        """
                        SELECT 1 FROM memories
                        WHERE scope = ? AND owner_id = ? AND key = ?
                        """,
                        (GLOBAL_SCOPE, GLOBAL_OWNER, normalized_key),
                    ).fetchone()
                    is not None
                )
            return cursor.rowcount > 0, fallback

        return self._run_write(operation)

    def count(self, scope: str, owner_id: str) -> int:
        normalized_scope, normalized_owner = self._scope_owner(scope, owner_id)

        def operation(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                "SELECT COUNT(*) FROM memories WHERE scope = ? AND owner_id = ?",
                (normalized_scope, normalized_owner),
            ).fetchone()
            return int(row[0]) if row is not None else 0

        return self._run_read(operation)
