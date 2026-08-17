"""SQLite persistence for payload-free Agent traces."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from workbench_core.agent import SpanRecord, TraceRecord


def initialize_trace_schema(connection: sqlite3.Connection) -> None:
    """Create additive Trace tables without changing conversation data."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            entrypoint TEXT NOT NULL,
            root_span_id TEXT NOT NULL,
            started_at_ms INTEGER NOT NULL,
            ended_at_ms INTEGER,
            duration_ms REAL,
            status TEXT NOT NULL,
            outcome TEXT,
            error_type TEXT,
            model TEXT,
            llm_call_count INTEGER NOT NULL DEFAULT 0,
            tool_call_count INTEGER NOT NULL DEFAULT 0,
            planning_call_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER,
            output_tokens INTEGER,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS trace_spans (
            span_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            parent_span_id TEXT,
            sequence_no INTEGER NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            started_at_ms INTEGER NOT NULL,
            ended_at_ms INTEGER,
            duration_ms REAL,
            status TEXT NOT NULL,
            error_type TEXT,
            attributes_json TEXT NOT NULL DEFAULT '{}',
            tool_name TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            FOREIGN KEY (trace_id) REFERENCES traces(trace_id),
            UNIQUE (trace_id, sequence_no)
        );
        CREATE INDEX IF NOT EXISTS idx_traces_session_started
            ON traces(session_id, started_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_traces_turn_started
            ON traces(turn_id, started_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_trace_spans_trace_sequence
            ON trace_spans(trace_id, sequence_no);
        """
    )


class SQLiteTraceStore:
    """Write and query redacted Trace records in Leon's session database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            initialize_trace_schema(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _require_session_id(trace: TraceRecord) -> str:
        if trace.session_id is None:
            raise ValueError("Leon trace persistence requires session_id")
        return trace.session_id

    @staticmethod
    def _require_session(connection: sqlite3.Connection, session_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Trace session does not exist")

    def start_trace(self, trace: TraceRecord) -> None:
        session_id = self._require_session_id(trace)
        with self._connect() as connection:
            self._require_session(connection, session_id)
            connection.execute(
                """
                INSERT INTO traces (
                    trace_id, turn_id, session_id, entrypoint, root_span_id,
                    started_at_ms, ended_at_ms, duration_ms, status, outcome,
                    error_type, model, llm_call_count, tool_call_count,
                    planning_call_count, input_tokens, output_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._trace_values(trace),
            )

    def start_span(self, span: SpanRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trace_spans (
                    span_id, trace_id, parent_span_id, sequence_no, kind, name,
                    started_at_ms, ended_at_ms, duration_ms, status, error_type,
                    attributes_json, tool_name, model, input_tokens, output_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._span_values(span),
            )

    def finish_span(self, span: SpanRecord) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE trace_spans
                SET parent_span_id = ?, sequence_no = ?, kind = ?, name = ?,
                    started_at_ms = ?, ended_at_ms = ?, duration_ms = ?, status = ?,
                    error_type = ?, attributes_json = ?, tool_name = ?, model = ?,
                    input_tokens = ?, output_tokens = ?
                WHERE span_id = ? AND trace_id = ?
                """,
                (
                    span.parent_span_id,
                    span.sequence_no,
                    span.kind,
                    span.name,
                    span.started_at_ms,
                    span.ended_at_ms,
                    span.duration_ms,
                    span.status,
                    span.error_type,
                    json.dumps(span.attributes, ensure_ascii=False, sort_keys=True),
                    span.tool_name,
                    span.model,
                    span.input_tokens,
                    span.output_tokens,
                    span.span_id,
                    span.trace_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Trace span was not started by this store")

    def finish_trace(self, trace: TraceRecord) -> None:
        session_id = self._require_session_id(trace)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE traces
                SET turn_id = ?, session_id = ?, entrypoint = ?, root_span_id = ?,
                    started_at_ms = ?, ended_at_ms = ?, duration_ms = ?, status = ?,
                    outcome = ?, error_type = ?, model = ?, llm_call_count = ?,
                    tool_call_count = ?, planning_call_count = ?, input_tokens = ?,
                    output_tokens = ?
                WHERE trace_id = ? AND session_id = ?
                """,
                (
                    trace.turn_id,
                    session_id,
                    trace.entrypoint,
                    trace.root_span_id,
                    trace.started_at_ms,
                    trace.ended_at_ms,
                    trace.duration_ms,
                    trace.status,
                    trace.outcome,
                    trace.error_type,
                    trace.model,
                    trace.llm_call_count,
                    trace.tool_call_count,
                    trace.planning_call_count,
                    trace.input_tokens,
                    trace.output_tokens,
                    trace.trace_id,
                    session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Trace was not started by this store")

    def list_traces(self, session_id: str, *, limit: int = 20) -> list[TraceRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM traces
                WHERE session_id = ?
                ORDER BY started_at_ms DESC, trace_id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [self._row_to_trace(row) for row in rows]

    def get_trace(
        self,
        session_id: str,
        trace_id: str,
    ) -> tuple[TraceRecord, list[SpanRecord]] | None:
        with self._connect() as connection:
            trace_row = connection.execute(
                "SELECT * FROM traces WHERE trace_id = ? AND session_id = ?",
                (trace_id, session_id),
            ).fetchone()
            if trace_row is None:
                return None
            span_rows = connection.execute(
                """
                SELECT * FROM trace_spans
                WHERE trace_id = ?
                ORDER BY sequence_no
                """,
                (trace_id,),
            ).fetchall()
        return self._row_to_trace(trace_row), [
            self._row_to_span(row) for row in span_rows
        ]

    @staticmethod
    def _trace_values(trace: TraceRecord) -> tuple[Any, ...]:
        return (
            trace.trace_id,
            trace.turn_id,
            trace.session_id,
            trace.entrypoint,
            trace.root_span_id,
            trace.started_at_ms,
            trace.ended_at_ms,
            trace.duration_ms,
            trace.status,
            trace.outcome,
            trace.error_type,
            trace.model,
            trace.llm_call_count,
            trace.tool_call_count,
            trace.planning_call_count,
            trace.input_tokens,
            trace.output_tokens,
        )

    @staticmethod
    def _span_values(span: SpanRecord) -> tuple[Any, ...]:
        return (
            span.span_id,
            span.trace_id,
            span.parent_span_id,
            span.sequence_no,
            span.kind,
            span.name,
            span.started_at_ms,
            span.ended_at_ms,
            span.duration_ms,
            span.status,
            span.error_type,
            json.dumps(span.attributes, ensure_ascii=False, sort_keys=True),
            span.tool_name,
            span.model,
            span.input_tokens,
            span.output_tokens,
        )

    @staticmethod
    def _row_to_trace(row: sqlite3.Row) -> TraceRecord:
        return TraceRecord(
            trace_id=str(row["trace_id"]),
            turn_id=str(row["turn_id"]),
            session_id=str(row["session_id"]),
            entrypoint=str(row["entrypoint"]),  # type: ignore[arg-type]
            root_span_id=str(row["root_span_id"]),
            started_at_ms=int(row["started_at_ms"]),
            ended_at_ms=(
                int(row["ended_at_ms"]) if row["ended_at_ms"] is not None else None
            ),
            duration_ms=(
                float(row["duration_ms"]) if row["duration_ms"] is not None else None
            ),
            status=str(row["status"]),  # type: ignore[arg-type]
            outcome=(
                str(row["outcome"]) if row["outcome"] is not None else None
            ),  # type: ignore[arg-type]
            error_type=(
                str(row["error_type"]) if row["error_type"] is not None else None
            ),
            model=str(row["model"]) if row["model"] is not None else None,
            llm_call_count=int(row["llm_call_count"]),
            tool_call_count=int(row["tool_call_count"]),
            planning_call_count=int(row["planning_call_count"]),
            input_tokens=(
                int(row["input_tokens"]) if row["input_tokens"] is not None else None
            ),
            output_tokens=(
                int(row["output_tokens"])
                if row["output_tokens"] is not None
                else None
            ),
        )

    @staticmethod
    def _row_to_span(row: sqlite3.Row) -> SpanRecord:
        return SpanRecord(
            span_id=str(row["span_id"]),
            trace_id=str(row["trace_id"]),
            parent_span_id=(
                str(row["parent_span_id"])
                if row["parent_span_id"] is not None
                else None
            ),
            sequence_no=int(row["sequence_no"]),
            kind=str(row["kind"]),  # type: ignore[arg-type]
            name=str(row["name"]),
            started_at_ms=int(row["started_at_ms"]),
            ended_at_ms=(
                int(row["ended_at_ms"]) if row["ended_at_ms"] is not None else None
            ),
            duration_ms=(
                float(row["duration_ms"]) if row["duration_ms"] is not None else None
            ),
            status=str(row["status"]),  # type: ignore[arg-type]
            error_type=(
                str(row["error_type"]) if row["error_type"] is not None else None
            ),
            attributes=json.loads(str(row["attributes_json"])),
            tool_name=(
                str(row["tool_name"]) if row["tool_name"] is not None else None
            ),
            model=str(row["model"]) if row["model"] is not None else None,
            input_tokens=(
                int(row["input_tokens"]) if row["input_tokens"] is not None else None
            ),
            output_tokens=(
                int(row["output_tokens"])
                if row["output_tokens"] is not None
                else None
            ),
        )
