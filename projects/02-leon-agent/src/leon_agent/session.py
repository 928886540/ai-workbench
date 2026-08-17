"""SQLite-backed conversation and tool-call history."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from workbench_core.agent import AgentResult, ToolStep

from leon_agent.trace_store import initialize_trace_schema


class SessionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    is_pinned INTEGER NOT NULL DEFAULT 0,
                    llm_provider TEXT,
                    llm_model TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    turn_id TEXT,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS message_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (message_id) REFERENCES messages(id)
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    generation_plan_id TEXT,
                    trace_id TEXT,
                    span_id TEXT,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS image_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    tool_call_id INTEGER NOT NULL,
                    generation_plan_id TEXT,
                    job_id TEXT NOT NULL,
                    status TEXT,
                    created_at INTEGER NOT NULL,
                    UNIQUE (session_id, job_id),
                    FOREIGN KEY (tool_call_id) REFERENCES tool_calls(id)
                );
                CREATE TABLE IF NOT EXISTS voice_clips (
                    clip_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    voice_id TEXT NOT NULL,
                    voice_name TEXT NOT NULL,
                    audio BLOB NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_voice_clips_session_created
                    ON voice_clips(session_id, created_at);
                """
            )
            initialize_trace_schema(connection)
            session_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(sessions)")
            }
            if "llm_provider" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN llm_provider TEXT")
            if "llm_model" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN llm_model TEXT")
            if "llm_base_url" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN llm_base_url TEXT")
            if "is_pinned" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0"
                )
            message_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(messages)")
            }
            if "turn_id" not in message_columns:
                connection.execute("ALTER TABLE messages ADD COLUMN turn_id TEXT")
            tool_call_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tool_calls)")
            }
            if "trace_id" not in tool_call_columns:
                connection.execute("ALTER TABLE tool_calls ADD COLUMN trace_id TEXT")
            if "span_id" not in tool_call_columns:
                connection.execute("ALTER TABLE tool_calls ADD COLUMN span_id TEXT")

    def create_session(self) -> str:
        session_id = uuid4().hex
        now = int(time.time() * 1000)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, ?, ?)",
                (session_id, now, now),
            )
        return session_id

    def has_session(self, session_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return row is not None

    def set_session_pinned(self, session_id: str, pinned: bool) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET is_pinned = ? WHERE id = ?",
                (int(pinned), session_id),
            )
        return cursor.rowcount > 0

    def get_provider_pin(self, session_id: str) -> tuple[str, str] | None:
        """Return the persisted (provider scope, base URL) pin for a session."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT llm_provider, llm_base_url FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        scope = str(row["llm_provider"] or "").strip()
        base_url = str(row["llm_base_url"] or "").strip()
        if not scope or not base_url:
            return None
        return scope, base_url

    def set_provider_pin(
        self,
        session_id: str,
        *,
        scope: str,
        base_url: str,
    ) -> None:
        """Persist provider identity + endpoint metadata; the API key is never stored."""
        normalized_scope = scope.strip()
        normalized_base_url = base_url.strip()
        if not normalized_scope or not normalized_base_url:
            raise ValueError("provider pin requires a scope and a base URL")
        now = int(time.time() * 1000)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT llm_provider FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Session not found: {session_id}")
            existing = str(row["llm_provider"] or "").strip()
            if existing and existing != normalized_scope:
                raise ValueError(
                    "Refusing to move a pinned session to a different provider"
                )
            connection.execute(
                """
                UPDATE sessions
                SET llm_provider = ?, llm_base_url = ?, updated_at = ?
                WHERE id = ?
                """,
                (normalized_scope, normalized_base_url, now, session_id),
            )

    def get_model_selection(self, session_id: str) -> tuple[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT llm_provider, llm_model FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Session not found: {session_id}")
        provider = str(row["llm_provider"] or "").strip()
        model = str(row["llm_model"] or "").strip()
        return (provider, model) if provider and model else None

    def set_model_selection(
        self,
        session_id: str,
        *,
        provider: str | None,
        model: str | None,
    ) -> None:
        normalized_provider = (provider or "").strip() or None
        normalized_model = (model or "").strip() or None
        if (normalized_provider is None) != (normalized_model is None):
            raise ValueError("provider and model must both be set or both be cleared")
        now = int(time.time() * 1000)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions
                SET llm_provider = ?, llm_model = ?, updated_at = ?
                WHERE id = ?
                """,
                (normalized_provider, normalized_model, now, session_id),
            )
        if cursor.rowcount == 0:
            raise ValueError(f"Session not found: {session_id}")

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        turn_id: str | None = None,
    ) -> None:
        now = int(time.time() * 1000)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (session_id, role, content, turn_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, role, content, turn_id, now),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )

    def add_voice_clip(
        self,
        session_id: str,
        *,
        text: str,
        voice_id: str,
        voice_name: str,
        audio: bytes,
    ) -> dict[str, Any]:
        """Persist a voice attachment without adding duplicate Agent context."""
        if not audio:
            raise ValueError("voice clip audio must not be empty")
        clip_id = uuid4().hex
        now = int(time.time() * 1000)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO voice_clips
                    (clip_id, session_id, text, voice_id, voice_name, audio, created_at)
                SELECT ?, id, ?, ?, ?, ?, ? FROM sessions WHERE id = ?
                """,
                (clip_id, text, voice_id, voice_name, audio, now, session_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Session not found: {session_id}")
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        return {
            "clip_id": clip_id,
            "text": text,
            "voice_id": voice_id,
            "voice_name": voice_name,
            "bytes": len(audio),
            "created_at": now,
        }

    def load_voice_clips(
        self,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Load attachment metadata only; audio remains behind the clip route."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT clip_id, text, voice_id, voice_name, bytes, created_at
                FROM (
                    SELECT
                        clip_id,
                        text,
                        voice_id,
                        voice_name,
                        length(audio) AS bytes,
                        created_at
                    FROM voice_clips
                    WHERE session_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                )
                ORDER BY created_at, clip_id
                """,
                (session_id, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_voice_clip_audio(self, clip_id: str) -> bytes | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT audio FROM voice_clips WHERE clip_id = ?",
                (clip_id,),
            ).fetchone()
        return bytes(row["audio"]) if row is not None else None

    def replace_latest_assistant(
        self,
        session_id: str,
        content: str,
        *,
        turn_id: str | None = None,
    ) -> None:
        """Replace the latest assistant answer while preserving its turn position."""
        now = int(time.time() * 1000)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM messages
                WHERE session_id = ? AND role = 'assistant'
                ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO messages (session_id, role, content, turn_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_id, "assistant", content, turn_id, now),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO message_revisions (message_id, content, created_at)
                    SELECT id, content, created_at FROM messages WHERE id = ?
                    """,
                    (int(row["id"]),),
                )
                connection.execute(
                    """
                    UPDATE messages
                    SET content = ?, turn_id = COALESCE(?, turn_id), created_at = ?
                    WHERE id = ?
                    """,
                    (content, turn_id, now, int(row["id"])),
                )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )

    def replace_latest_user(
        self,
        session_id: str,
        content: str,
        *,
        turn_id: str | None = None,
    ) -> None:
        """Update the latest user prompt when retrying an edited current turn."""
        now = int(time.time() * 1000)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM messages
                WHERE session_id = ? AND role = 'user'
                ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO messages (session_id, role, content, turn_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_id, "user", content, turn_id, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE messages
                    SET content = ?, turn_id = COALESCE(?, turn_id), created_at = ?
                    WHERE id = ?
                    """,
                    (content, turn_id, now, int(row["id"])),
                )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )

    def load_messages(
        self,
        session_id: str,
        *,
        limit: int = 30,
        include_created_at: bool = False,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, turn_id, created_at
                FROM (
                    SELECT id, role, content, turn_id, created_at
                    FROM messages
                    WHERE session_id = ? AND role IN ('user', 'assistant')
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id
                """,
                (session_id, limit),
            ).fetchall()
            revisions_by_message: dict[int, list[dict[str, Any]]] = {}
            if include_created_at and rows:
                message_ids = [int(row["id"]) for row in rows]
                placeholders = ", ".join("?" for _ in message_ids)
                revision_rows = connection.execute(
                    f"""
                    SELECT message_id, content, created_at
                    FROM message_revisions
                    WHERE message_id IN ({placeholders})
                    ORDER BY id
                    """,  # noqa: S608 - placeholders are generated, values stay bound
                    message_ids,
                ).fetchall()
                for revision in revision_rows:
                    revisions_by_message.setdefault(
                        int(revision["message_id"]), []
                    ).append(
                        {
                            "content": str(revision["content"]),
                            "created_at": int(revision["created_at"]),
                        }
                    )

        # Failed CLI turns used to be persisted as assistant messages. Exclude
        # those old pairs so a transport error/request id cannot be replayed as
        # conversation context after the user switches models.
        messages: list[dict[str, Any]] = []
        index = 0
        while index < len(rows):
            row = rows[index]
            is_error = row["role"] == "assistant" and str(row["content"]).startswith(
                ("请求失败：", "请求失败:")
            )
            next_is_error = (
                index + 1 < len(rows)
                and rows[index + 1]["role"] == "assistant"
                and str(rows[index + 1]["content"]).startswith(("请求失败：", "请求失败:"))
            )
            if is_error:
                index += 1
                continue
            if row["role"] == "user" and next_is_error:
                index += 2
                continue
            message: dict[str, Any] = {"role": row["role"], "content": row["content"]}
            if include_created_at and row["created_at"] is not None:
                message["id"] = int(row["id"])
                message["created_at"] = int(row["created_at"])
                message["turn_id"] = (
                    str(row["turn_id"]) if row["turn_id"] is not None else None
                )
                message["revisions"] = revisions_by_message.get(int(row["id"]), [])
            messages.append(message)
            index += 1
        return messages

    def record_result(self, session_id: str, result: AgentResult) -> None:
        for step in result.steps:
            self._record_tool_step(
                session_id,
                step,
                fallback_trace_id=result.trace_id,
            )

    def _record_tool_step(
        self,
        session_id: str,
        step: ToolStep,
        *,
        fallback_trace_id: str | None = None,
    ) -> None:
        now = int(time.time() * 1000)
        plan_id = step.result.get("generation_plan_id")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tool_calls
                    (session_id, name, arguments_json, result_json, generation_plan_id,
                     trace_id, span_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    step.name,
                    json.dumps(step.arguments, ensure_ascii=False),
                    json.dumps(step.result, ensure_ascii=False),
                    plan_id,
                    step.trace_id or fallback_trace_id,
                    step.span_id,
                    now,
                ),
            )
            tool_call_id = int(cursor.lastrowid)
            for job in step.result.get("jobs", []):
                if not isinstance(job, dict) or not job.get("job_id"):
                    continue
                connection.execute(
                    """
                    INSERT OR REPLACE INTO image_jobs
                        (session_id, tool_call_id, generation_plan_id, job_id, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        tool_call_id,
                        plan_id,
                        job["job_id"],
                        job.get("status"),
                        now,
                    ),
                )

    def list_sessions(self, *, limit: int = 10) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    s.id,
                    s.created_at,
                    s.updated_at,
                    s.is_pinned,
                    (
                        SELECT COUNT(*)
                        FROM messages AS counted
                        WHERE counted.session_id = s.id
                    ) AS message_count,
                    (
                        SELECT topic.content
                        FROM messages AS topic
                        WHERE topic.session_id = s.id AND topic.role = 'user'
                        ORDER BY topic.id ASC
                        LIMIT 1
                    ) AS topic
                FROM sessions AS s
                ORDER BY s.is_pinned DESC, s.updated_at DESC, s.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        sessions = []
        for row in rows:
            item = dict(row)
            topic = " ".join(str(item.pop("topic") or "").split())
            item["title"] = topic[:80] or "新会话"
            item["pinned"] = bool(item.pop("is_pinned"))
            sessions.append(item)
        return sessions
