from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

from leon_agent.memory.service import MemoryService
from leon_agent.memory.store import MemoryStore
from leon_agent.memory.tools import create_memory_tools
from leon_agent.session import SessionStore
from workbench_core.agent import AgentEvent, AgentRuntime, ToolRegistry
from workbench_core.llm import ChatTurn, ToolCall

RAW_VALUE = "cinematic-raw-marker-7f3a"


class MemoryFlowClient:
    """Fake provider that writes a memory, reads it back, then answers."""

    def __init__(self) -> None:
        self.requests: list[list[dict[str, Any]]] = []
        self.calls = 0

    def chat_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> ChatTurn:
        del tools, temperature
        self.calls += 1
        self.requests.append(deepcopy(messages))
        if self.calls == 1:
            arguments = json.dumps(
                {
                    "scope": "user",
                    "key": "image.preference",
                    "value": {"style": RAW_VALUE},
                }
            )
            return self._tool_call("memory_upsert", arguments, "upsert-call")
        if self.calls == 2:
            arguments = json.dumps(
                {
                    "scope": "effective",
                    "key": "image.preference",
                    "limit": 1,
                }
            )
            return self._tool_call("memory_get", arguments, "get-call")
        return ChatTurn(
            content="已保存并读取偏好。",
            raw_message={"role": "assistant", "content": "已保存并读取偏好。"},
        )

    @staticmethod
    def _tool_call(name: str, arguments: str, call_id: str) -> ChatTurn:
        return ChatTurn(
            content=None,
            tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
            raw_message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                ],
            },
        )


def test_memory_raw_value_stays_in_llm_transcript_not_audit(tmp_path: Path) -> None:
    db_path = tmp_path / "memory-audit.db"
    memory_store = MemoryStore(db_path)
    session_store = SessionStore(db_path)
    session_id = session_store.create_session()
    service = MemoryService(
        memory_store,
        session_id=session_id,
        clock=lambda: 1_700_000_000_000,
    )
    tools = ToolRegistry(
        create_memory_tools(
            service,
            user_message_provider=lambda: "记住我喜欢电影感照片",
            writes_used_provider=lambda: 0,
        )
    )
    events: list[AgentEvent] = []
    client = MemoryFlowClient()
    runtime = AgentRuntime(
        client=client,  # type: ignore[arg-type]
        tools=tools,
        system_prompt="Use explicit memory tools when requested.",
        on_event=events.append,
    )

    result = runtime.run("请按我的明确请求保存并读取偏好")

    # The real handler persisted the value, and the following read returned it
    # only to the in-memory LLM transcript.
    record = memory_store.get("user", "local-owner", "image.preference")
    assert record is not None
    assert record.value == {"style": RAW_VALUE}
    tool_messages = [
        message
        for message in client.requests[-1]
        if message.get("role") == "tool"
    ]
    assert any(RAW_VALUE in str(message.get("content")) for message in tool_messages)

    # Runtime events and ToolStep are the redacted audit view, never raw memory.
    assert RAW_VALUE not in repr(events)
    assert RAW_VALUE not in repr(result.steps)
    assert [step.name for step in result.steps] == ["memory_upsert", "memory_get"]
    assert result.steps[0].arguments == {
        "scope": "user",
        "key": "image.preference",
    }
    assert result.steps[0].result == {
        "ok": True,
        "created": True,
        "updated_at": 1_700_000_000_000,
    }
    assert result.steps[1].arguments == {
        "scope": "effective",
        "key": "image.preference",
        "limit": 1,
    }
    assert result.steps[1].result == {
        "scope": "effective",
        "count": 1,
        "memories": [{"scope": "user", "key": "image.preference"}],
    }

    # SessionStore receives the actual AgentResult, so this checks the persisted
    # boundary rather than merely re-checking the in-memory projection.
    session_store.record_result(session_id, result)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT name, arguments_json, result_json
            FROM tool_calls
            WHERE session_id = ?
            ORDER BY id
            """,
            (session_id,),
        ).fetchall()

    assert len(rows) == 2
    assert all(RAW_VALUE not in f"{arguments}{result_json}" for _, arguments, result_json in rows)
    assert json.loads(rows[0][1]) == result.steps[0].arguments
    assert json.loads(rows[0][2]) == result.steps[0].result
    assert json.loads(rows[1][1]) == result.steps[1].arguments
    assert json.loads(rows[1][2]) == result.steps[1].result
