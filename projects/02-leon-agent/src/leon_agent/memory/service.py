"""Pure memory policy and a safe service facade for the Phase 1 MVP."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from leon_agent.memory.store import (
    DEFAULT_PRINCIPAL,
    EFFECTIVE_SCOPE,
    GLOBAL_OWNER,
    GLOBAL_SCOPE,
    MAX_GLOBAL_MEMORIES,
    MAX_USER_MEMORIES,
    SQLITE_INTEGER_MAX,
    SQLITE_INTEGER_MIN,
    USER_SCOPE,
    MemoryRecord,
    MemoryStore,
    MemoryStoreError,
    normalize_key,
    normalize_scope,
    normalize_value,
)

SOURCE_EXPLICIT_USER = "explicit_user_request"
MAX_GET_LIMIT = 20
MAX_CONTEXT_RECORDS = 12
MAX_CONTEXT_CHARS = 2_400
MAX_CONTEXT_VALUE_CHARS = 512


@dataclass
class _MemoryTurnState:
    user_message: str
    writes_used: int = 0


_CURRENT_TURN: ContextVar[_MemoryTurnState | None] = ContextVar(
    "leon_memory_turn",
    default=None,
)


@contextmanager
def memory_turn(user_message: str) -> Iterator[None]:
    """Bind explicit consent and the single-write budget to one Agent turn."""

    state = _MemoryTurnState(user_message=user_message)
    token = _CURRENT_TURN.set(state)
    try:
        yield
    finally:
        _CURRENT_TURN.reset(token)


def current_memory_user_message() -> str | None:
    state = _CURRENT_TURN.get()
    return state.user_message if state is not None else None


def current_memory_writes_used() -> int:
    state = _CURRENT_TURN.get()
    return state.writes_used if state is not None else 0


def claim_memory_write() -> int:
    """Consume the current turn's one-write budget and return its prior count."""

    state = _CURRENT_TURN.get()
    if state is None:
        return 0
    writes_used = state.writes_used
    state.writes_used += 1
    return writes_used


class MemoryPolicyError(ValueError):
    """A stable, non-sensitive policy error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class MemoryIntent:
    """The conservative intent classification used by the write gate."""

    operation: str
    explicit: bool
    global_requested: bool


_UPSERT_MARKERS = (
    "记住",
    "别忘了",
    "保存为记忆",
    "保存为偏好",
    "存为偏好",
    "保存一下",
    "记下来",
    "以后默认",
    "默认使用",
    "remember",
    "save this",
    "save that",
    "keep this",
)
_DELETE_MARKERS = (
    "忘掉",
    "忘记这条记忆",
    "删除记忆",
    "删除这条",
    "删掉记忆",
    "清除记忆",
    "不再记住",
    "forget",
    "delete this memory",
    "remove memory",
)
_NEGATIVE_MARKERS = (
    "不要记住",
    "别记住",
    "不用记住",
    "不要保存",
    "别保存",
    "无需保存",
    "不要存",
    "别存",
    "don't remember",
    "do not remember",
    "don't save",
    "do not save",
)
_NEGATIVE_CJK_PATTERN = re.compile(
    r"(?:不想|不希望|不愿意?|不需要|不要|别|不用|无需)"
    r"(?:(?![。！？!?；;\n]).){0,16}?"
    r"(?:记住|保存|存(?:下|为)?|记下来)"
)
_NEGATIVE_EN_PATTERN = re.compile(
    r"(?:do not|don't|dont)\s+(?:want|need)\b"
    r"(?:(?![.!?;\n]).){0,40}?\b(?:remember|save)\b",
    re.IGNORECASE,
)
_GLOBAL_MARKERS = (
    "全局",
    "所有会话",
    "每个会话",
    "global",
    "all sessions",
    "every session",
    "across sessions",
)


def _contains_marker(message: str, markers: tuple[str, ...]) -> bool:
    folded = message.casefold()
    return any(marker.casefold() in folded for marker in markers)


def _contains_negative_intent(message: str) -> bool:
    return bool(
        _contains_marker(message, _NEGATIVE_MARKERS)
        or _NEGATIVE_CJK_PATTERN.search(message)
        or _NEGATIVE_EN_PATTERN.search(message)
    )


def detect_memory_intent(message: Any) -> MemoryIntent:
    """Classify explicit save/delete language; ordinary text defaults to none."""

    if not isinstance(message, str):
        return MemoryIntent("none", False, False)
    normalized = message.strip()
    if not normalized:
        return MemoryIntent("none", False, False)
    if _contains_negative_intent(normalized):
        return MemoryIntent("none", False, _contains_marker(normalized, _GLOBAL_MARKERS))
    wants_upsert = _contains_marker(normalized, _UPSERT_MARKERS)
    wants_delete = _contains_marker(normalized, _DELETE_MARKERS)
    global_requested = _contains_marker(normalized, _GLOBAL_MARKERS)
    if wants_upsert and wants_delete:
        return MemoryIntent("ambiguous", True, global_requested)
    if wants_upsert:
        return MemoryIntent("upsert", True, global_requested)
    if wants_delete:
        return MemoryIntent("delete", True, global_requested)
    return MemoryIntent("none", False, global_requested)


def authorize_memory_write(
    message: Any,
    operation: str,
    *,
    scope: str = USER_SCOPE,
    writes_used: int = 0,
) -> MemoryIntent:
    """Authorize exactly one explicit write/delete operation for a turn.

    This function is intentionally pure. Phase 2 may put its result in a
    per-turn context, but the policy itself does not know about AgentRuntime.
    """

    normalized_scope = normalize_scope(scope)
    if operation not in {"upsert", "delete"}:
        raise MemoryPolicyError("invalid_operation", "memory operation is invalid.")
    if isinstance(writes_used, bool) or not isinstance(writes_used, int) or writes_used < 0:
        raise MemoryPolicyError("invalid_write_count", "write count is invalid.")
    if writes_used >= 1:
        raise MemoryPolicyError("write_limit_reached", "only one memory write is allowed per turn.")
    intent = detect_memory_intent(message)
    if intent.operation == "ambiguous":
        raise MemoryPolicyError("ambiguous_intent", "memory intent is ambiguous.")
    if intent.operation != operation:
        raise MemoryPolicyError(
            "explicit_consent_required",
            "an explicit memory instruction is required.",
        )
    if normalized_scope == GLOBAL_SCOPE and not intent.global_requested:
        raise MemoryPolicyError(
            "global_scope_requires_explicit",
            "global memory requires an explicit global or all-sessions instruction.",
        )
    return intent


_SENSITIVE_NAME_PATTERN = re.compile(
    r"(?:password|passwd|passphrase|secret|token|api[_-]?key|access[_-]?key|"
    r"refresh[_-]?token|authorization|cookie|private[_-]?key|recovery[_-]?code|"
    r"phone|mobile|email|银行卡|信用卡|身份证|社保|密码|密钥|私钥|验证码)",
    re.IGNORECASE,
)
_PEM_PATTERN = re.compile(r"-----BEGIN(?: [^-]+)? PRIVATE KEY-----", re.IGNORECASE)
_BEARER_PATTERN = re.compile(r"\bbearer\s+[a-z0-9._~+/=-]{16,}", re.IGNORECASE)
_JWT_PATTERN = re.compile(
    r"^[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}$",
    re.IGNORECASE,
)
_API_KEY_PATTERN = re.compile(
    r"(?:sk-[a-z0-9]{16,}|tvly-[a-z0-9]{12,}|gh[pousr]_[a-z0-9_]{12,}|"
    r"github_pat_[a-z0-9_]{12,}|xox[baprs]-[a-z0-9-]{12,}|akia[a-z0-9]{12,})",
    re.IGNORECASE,
)
_URL_CREDENTIAL_PATTERN = re.compile(r"://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)
_LONG_DIGIT_PATTERN = re.compile(r"^\d{11,19}$")


def _sensitive_string(value: str) -> bool:
    stripped = value.strip()
    return bool(
        _PEM_PATTERN.search(stripped)
        or _BEARER_PATTERN.search(stripped)
        or _JWT_PATTERN.fullmatch(stripped)
        or _API_KEY_PATTERN.search(stripped)
        or _URL_CREDENTIAL_PATTERN.search(stripped)
        or _LONG_DIGIT_PATTERN.fullmatch(stripped)
    )


def contains_sensitive_value(value: Mapping[str, Any]) -> bool:
    """Recursively detect high-risk field names and common secret formats."""

    def walk(node: Any) -> bool:
        if isinstance(node, Mapping):
            for key, child in node.items():
                if isinstance(key, str) and _SENSITIVE_NAME_PATTERN.search(key):
                    return True
                if walk(child):
                    return True
            return False
        if isinstance(node, list):
            return any(walk(child) for child in node)
        return isinstance(node, str) and _sensitive_string(node)

    return walk(value)


def contains_sensitive_key(key: str) -> bool:
    """Check a normalized memory key without exposing it in an error."""

    return bool(_SENSITIVE_NAME_PATTERN.search(key))


def _safe_error(error: MemoryStoreError | MemoryPolicyError) -> dict[str, Any]:
    return {"ok": False, "error_code": error.code, "error": error.message}


def _public_record(record: MemoryRecord) -> dict[str, Any]:
    return {
        "scope": record.scope,
        "key": record.key,
        "value": record.value,
        "source_kind": record.source_kind,
        "source_session_id": record.source_session_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "untrusted_data": True,
    }


class MemoryService:
    """Validate policy and expose a stable, tool-friendly memory facade."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        session_id: str,
        principal: str = DEFAULT_PRINCIPAL,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if principal != DEFAULT_PRINCIPAL:
            raise MemoryPolicyError("invalid_principal", "only the local owner is supported.")
        if not isinstance(session_id, str) or not session_id.strip():
            raise MemoryPolicyError("invalid_session", "session_id must be a non-empty string.")
        self.store = store
        self.session_id = session_id.strip()
        self.principal = principal
        self._clock = clock or (lambda: int(time.time() * 1000))

    def upsert(
        self,
        key: str,
        value: Mapping[str, Any],
        scope: str = USER_SCOPE,
        *,
        user_message: str | None = None,
        writes_used: int = 0,
    ) -> dict[str, Any]:
        try:
            normalized_scope = normalize_scope(scope)
            authorize_memory_write(
                user_message,
                "upsert",
                scope=normalized_scope,
                writes_used=writes_used,
            )
            normalized_key = normalize_key(key)
            if contains_sensitive_key(normalized_key):
                raise MemoryPolicyError(
                    "sensitive_value_rejected",
                    "memory value rejected by privacy policy.",
                )
            normalized_value = normalize_value(value)
            if contains_sensitive_value(normalized_value):
                raise MemoryPolicyError(
                    "sensitive_value_rejected",
                    "memory value rejected by privacy policy.",
                )
            write = self.store.upsert(
                normalized_scope,
                self.principal,
                normalized_key,
                normalized_value,
                source_kind=SOURCE_EXPLICIT_USER,
                source_session_id=self.session_id,
                now_ms=self._timestamp(),
            )
            record = write.record
            return {
                "ok": True,
                "created": write.created,
                "scope": record.scope,
                "key": record.key,
                "source_kind": record.source_kind,
                "source_session_id": record.source_session_id,
                "updated_at": record.updated_at,
            }
        except (MemoryStoreError, MemoryPolicyError) as exc:
            return _safe_error(exc)

    def get(
        self,
        *,
        key: str | None = None,
        prefix: str | None = None,
        scope: str = EFFECTIVE_SCOPE,
        limit: int = 20,
    ) -> dict[str, Any]:
        try:
            normalized_scope = normalize_scope(scope, allow_effective=True)
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= MAX_GET_LIMIT
            ):
                raise MemoryStoreError("invalid_limit", "limit must be between 1 and 20.")
            if normalized_scope == EFFECTIVE_SCOPE:
                records = self.store.list_effective(
                    self.principal,
                    key=key,
                    prefix=prefix,
                    limit=limit,
                )
            else:
                owner = GLOBAL_OWNER if normalized_scope == GLOBAL_SCOPE else self.principal
                records = self.store.list(
                    normalized_scope,
                    owner,
                    key=key,
                    prefix=prefix,
                    limit=limit,
                )
            return {
                "ok": True,
                "scope": normalized_scope,
                "memories": [_public_record(record) for record in records],
                "count": len(records),
                "untrusted_data": True,
            }
        except MemoryStoreError as exc:
            return _safe_error(exc)

    def build_context(self) -> str:
        """Render a bounded, explicitly untrusted context for the current turn.

        Values that do not fit the per-record budget are represented by metadata
        only; the complete value remains available through ``memory_get``.
        Storage failures fail closed so ordinary chat keeps working.
        """

        try:
            global_records = self.store.list(
                GLOBAL_SCOPE,
                GLOBAL_OWNER,
                limit=MAX_GLOBAL_MEMORIES,
            )
            user_records = self.store.list(
                USER_SCOPE,
                self.principal,
                limit=MAX_USER_MEMORIES,
            )
        except (MemoryStoreError, sqlite3.Error):
            return ""

        effective = {record.key: record for record in global_records}
        effective.update({record.key: record for record in user_records})
        records = sorted(
            effective.values(),
            key=lambda record: (
                0 if record.scope == USER_SCOPE else 1,
                -record.updated_at,
                record.key,
            ),
        )

        opening = '<leon_memory_context untrusted_data="true">'
        closing = "</leon_memory_context>"
        lines: list[str] = []
        for record in records[:MAX_CONTEXT_RECORDS]:
            value_json = json.dumps(
                record.value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(value_json) > MAX_CONTEXT_VALUE_CHARS:
                line = (
                    f"- scope={record.scope} key={record.key} "
                    "value_omitted=true; call memory_get for the full value"
                )
            else:
                # Keep untrusted data from closing the surrounding marker or
                # being interpreted as markup by the provider.
                escaped = (
                    value_json.replace("&", r"\u0026")
                    .replace("<", r"\u003c")
                    .replace(">", r"\u003e")
                )
                line = f"- scope={record.scope} key={record.key} value={escaped}"
            candidate = "\n".join([opening, *lines, line, closing])
            if len(candidate) > MAX_CONTEXT_CHARS:
                break
            lines.append(line)
        if not lines:
            return ""
        return "\n".join([opening, *lines, closing])

    def delete(
        self,
        key: str,
        scope: str = USER_SCOPE,
        *,
        user_message: str | None = None,
        writes_used: int = 0,
    ) -> dict[str, Any]:
        try:
            normalized_scope = normalize_scope(scope)
            authorize_memory_write(
                user_message,
                "delete",
                scope=normalized_scope,
                writes_used=writes_used,
            )
            owner = GLOBAL_OWNER if normalized_scope == GLOBAL_SCOPE else self.principal
            deleted, fallback = self.store.delete_with_fallback(normalized_scope, owner, key)
            return {
                "ok": True,
                "scope": normalized_scope,
                "key": normalize_key(key),
                "deleted": deleted,
                "global_fallback": fallback,
            }
        except (MemoryStoreError, MemoryPolicyError) as exc:
            return _safe_error(exc)

    def _timestamp(self) -> int:
        try:
            timestamp = self._clock()
        except (OverflowError, TypeError, ValueError) as exc:
            raise MemoryPolicyError(
                "invalid_timestamp", "memory clock returned an invalid value."
            ) from exc
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < SQLITE_INTEGER_MIN
            or timestamp > SQLITE_INTEGER_MAX
        ):
            raise MemoryPolicyError("invalid_timestamp", "memory clock returned an invalid value.")
        return timestamp
