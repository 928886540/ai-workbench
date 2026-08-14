"""SQLite-backed conversation and tool-call history."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from workbench_core.agent import AgentResult, ToolStep


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
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    generation_plan_id TEXT,
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
                """
            )

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

    def add_message(self, session_id: str, role: str, content: str) -> None:
        now = int(time.time() * 1000)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, now),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )

    def load_messages(self, session_id: str, *, limit: int = 30) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content
                FROM (
                    SELECT id, role, content
                    FROM messages
                    WHERE session_id = ? AND role IN ('user', 'assistant')
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id
                """,
                (session_id, limit),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def record_result(self, session_id: str, result: AgentResult) -> None:
        for step in result.steps:
            self._record_tool_step(session_id, step)

    def _record_tool_step(self, session_id: str, step: ToolStep) -> None:
        now = int(time.time() * 1000)
        plan_id = step.result.get("generation_plan_id")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tool_calls
                    (session_id, name, arguments_json, result_json, generation_plan_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    step.name,
                    json.dumps(step.arguments, ensure_ascii=False),
                    json.dumps(step.result, ensure_ascii=False),
                    plan_id,
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
                SELECT s.id, s.created_at, s.updated_at, COUNT(m.id) AS message_count
                FROM sessions AS s
                LEFT JOIN messages AS m ON m.session_id = s.id
                GROUP BY s.id
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
