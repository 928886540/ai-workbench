from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from leon_agent.session import SessionStore
from leon_agent.trace_store import SQLiteTraceStore
from workbench_core.agent import (
    AgentResult,
    InMemoryTraceSink,
    ToolStep,
    TraceContext,
    TraceRecorder,
)


def _session_database(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "leon.db"
    sessions = SessionStore(path)
    session_id = sessions.create_session()
    sessions.add_message(session_id, "user", "keep this message")
    return path, session_id


def test_sqlite_trace_store_round_trips_trace_tree(tmp_path: Path) -> None:
    path, session_id = _session_database(tmp_path)
    store = SQLiteTraceStore(path)
    context = TraceContext.create(session_id=session_id, entrypoint="web")
    recorder = TraceRecorder(context, store)

    iteration_id = recorder.start_span(
        "iteration",
        "agent.iteration",
        attributes={"iteration": 1},
    )
    llm_id = recorder.start_span(
        "llm",
        "llm.request",
        parent_span_id=iteration_id,
        attributes={"requested_model": "model-a", "message_count": 2},
    )
    recorder.finish_span(
        llm_id,
        model="model-a",
        input_tokens=7,
        output_tokens=3,
    )
    tool_id = recorder.start_span(
        "tool",
        "tool.call",
        parent_span_id=iteration_id,
        tool_name="read_file",
    )
    recorder.finish_span(tool_id, status="error", error_type="tool_error")
    recorder.finish_span(iteration_id)
    expected_trace = recorder.finish_trace(status="ok", outcome="answered")

    reopened = SQLiteTraceStore(path)
    loaded = reopened.get_trace(session_id, context.trace_id)

    assert loaded is not None
    trace, spans = loaded
    assert trace == expected_trace
    assert [span.kind for span in spans] == ["agent", "iteration", "llm", "tool"]
    assert [span.sequence_no for span in spans] == [1, 2, 3, 4]
    assert next(span for span in spans if span.span_id == llm_id).parent_span_id == iteration_id
    assert next(span for span in spans if span.span_id == tool_id).status == "error"
    assert SessionStore(path).load_messages(session_id)[0]["content"] == "keep this message"


def test_sqlite_trace_store_enforces_session_ownership(tmp_path: Path) -> None:
    path, session_id = _session_database(tmp_path)
    sessions = SessionStore(path)
    other_session_id = sessions.create_session()
    store = SQLiteTraceStore(path)
    context = TraceContext.create(session_id=session_id, entrypoint="cli")
    TraceRecorder(context, store).finish_trace(status="ok", outcome="answered")

    assert len(store.list_traces(session_id)) == 1
    assert store.list_traces(other_session_id) == []
    assert store.get_trace(other_session_id, context.trace_id) is None


def test_sqlite_trace_store_never_persists_unknown_payload_attributes(
    tmp_path: Path,
) -> None:
    path, session_id = _session_database(tmp_path)
    store = SQLiteTraceStore(path)
    marker = "private-prompt-must-not-enter-trace"
    recorder = TraceRecorder(
        TraceContext.create(session_id=session_id, entrypoint="eval"),
        store,
    )
    span_id = recorder.start_span(
        "llm",
        "llm.request",
        attributes={"prompt": marker, "message_count": 1},
    )
    recorder.finish_span(span_id)
    recorder.finish_trace(status="ok", outcome="answered")

    assert marker.encode("utf-8") not in path.read_bytes()
    loaded = store.get_trace(session_id, recorder.context.trace_id)
    assert loaded is not None
    assert next(span for span in loaded[1] if span.span_id == span_id).attributes == {
        "message_count": 1
    }


def test_sqlite_trace_store_rejects_missing_session_and_invalid_limit(
    tmp_path: Path,
) -> None:
    path, _ = _session_database(tmp_path)
    store = SQLiteTraceStore(path)
    memory = InMemoryTraceSink()
    recorder = TraceRecorder(TraceContext.create(entrypoint="direct"), memory)
    recorder.finish_trace(status="ok", outcome="answered")

    with pytest.raises(ValueError, match="requires session_id"):
        store.start_trace(memory.traces[0])
    with pytest.raises(ValueError, match="limit"):
        store.list_traces("session", limit=0)


def test_trace_schema_is_additive_and_indexed(tmp_path: Path) -> None:
    path, _ = _session_database(tmp_path)
    SQLiteTraceStore(path)

    with sqlite3.connect(path) as connection:
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

    assert {"sessions", "messages", "traces", "trace_spans"} <= tables
    assert {
        "idx_traces_session_started",
        "idx_traces_turn_started",
        "idx_trace_spans_trace_sequence",
    } <= indexes


def test_session_store_persists_message_and_tool_correlations(tmp_path: Path) -> None:
    path, session_id = _session_database(tmp_path)
    store = SessionStore(path)
    context = TraceContext.create(session_id=session_id, entrypoint="web")
    span_id = "a" * 16

    store.add_message(
        session_id,
        "user",
        "correlated message",
        turn_id=context.turn_id,
    )
    store.record_result(
        session_id,
        AgentResult(
            answer="done",
            steps=[
                ToolStep(
                    "probe",
                    {"safe": True},
                    {"ok": True},
                    trace_id=context.trace_id,
                    span_id=span_id,
                )
            ],
            trace_id=context.trace_id,
            turn_id=context.turn_id,
        ),
    )

    messages = store.load_messages(session_id, include_created_at=True)
    assert messages[0]["turn_id"] is None
    assert messages[1]["turn_id"] == context.turn_id
    with sqlite3.connect(path) as connection:
        tool_row = connection.execute(
            "SELECT trace_id, span_id FROM tool_calls WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    assert tool_row == (context.trace_id, span_id)


def test_session_store_migrates_legacy_correlation_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                generation_plan_id TEXT,
                created_at INTEGER NOT NULL
            );
            INSERT INTO sessions (id, created_at, updated_at) VALUES ('legacy', 1, 1);
            INSERT INTO messages (session_id, role, content, created_at)
                VALUES ('legacy', 'user', 'old message', 1);
            INSERT INTO tool_calls (
                session_id, name, arguments_json, result_json, created_at
            ) VALUES ('legacy', 'probe', '{}', '{"ok": true}', 1);
            """
        )

    store = SessionStore(path)

    with sqlite3.connect(path) as connection:
        message_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(messages)")
        }
        tool_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_calls)")
        }
        old_message_turn = connection.execute(
            "SELECT turn_id FROM messages WHERE session_id = 'legacy'"
        ).fetchone()
        old_tool_trace = connection.execute(
            "SELECT trace_id, span_id FROM tool_calls WHERE session_id = 'legacy'"
        ).fetchone()

    assert "turn_id" in message_columns
    assert {"trace_id", "span_id"} <= tool_columns
    assert old_message_turn == (None,)
    assert old_tool_trace == (None, None)
    assert store.load_messages("legacy") == [{"role": "user", "content": "old message"}]
