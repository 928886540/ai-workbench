from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from leon_framework.checkpointing import (
    CheckpointSecurityError,
    checkpoint_key_path,
    create_thread_id,
    normalize_thread_id,
    open_encrypted_sqlite_checkpointer,
)
from leon_framework.graph import build_leon_graph
from workbench_core.agent import AgentTool, ToolRegistry


class SecretToolModel:
    final_answer = "AI_ANSWER_SECRET_J2V6"

    def bind_tools(self, tools: list[Any]) -> SecretToolModel:
        assert [tool.name for tool in tools] == ["read_file"]
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content=self.final_answer)
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"root_id": "workspace", "relative_path": "secret.txt"},
                    "id": "read-secret",
                    "type": "tool_call",
                }
            ],
        )


def _secret_registry(secret: str) -> ToolRegistry:
    return ToolRegistry(
        [
            AgentTool(
                name="read_file",
                description="Read one fixture file.",
                parameters={
                    "type": "object",
                    "properties": {
                        "root_id": {"type": "string"},
                        "relative_path": {"type": "string"},
                    },
                    "required": ["root_id", "relative_path"],
                    "additionalProperties": False,
                },
                handler=lambda **_: {"ok": True, "content": secret},
            )
        ]
    )


def _checkpoint_files(database_path: Path) -> list[Path]:
    return [
        path
        for path in database_path.parent.glob(f"{database_path.name}*")
        if path.is_file()
    ]


def test_thread_ids_are_opaque_and_strictly_validated() -> None:
    generated = create_thread_id()

    assert re.fullmatch(r"lg-[0-9a-f]{32}", generated)
    assert normalize_thread_id(f"  {generated}  ") == generated
    assert normalize_thread_id("interview_demo-1") == "interview_demo-1"
    for invalid in ("", "contains space", "../escape", "a" * 65, "中文"):
        with pytest.raises(ValueError, match="thread id"):
            normalize_thread_id(invalid)


def test_encrypted_sqlite_checkpoint_hides_raw_state_and_reopens(tmp_path) -> None:
    database_path = tmp_path / "checkpoints.db"
    secret = "FILE_BODY_SECRET_X9P7"
    user_text = "USER_MESSAGE_SECRET_K4M2"
    metadata_secret = "METADATA_SECRET_R8W3"
    memory_context_secret = "MEMORY_CONTEXT_SECRET_F6Q1"
    context_calls: list[bool] = []
    config = {
        "configurable": {"thread_id": "lg-security-proof"},
        "metadata": {"unsafe_user_label": metadata_secret},
    }

    def memory_context() -> str:
        context_calls.append(True)
        return memory_context_secret

    with open_encrypted_sqlite_checkpointer(database_path) as checkpointer:
        graph = build_leon_graph(
            SecretToolModel(),
            _secret_registry(secret),
            system_context_provider=memory_context,
            checkpointer=checkpointer,
        )
        graph.invoke({"messages": [HumanMessage(content=user_text)]}, config)

        for path in _checkpoint_files(database_path):
            persisted = path.read_bytes()
            assert secret.encode() not in persisted
            assert user_text.encode() not in persisted
            assert metadata_secret.encode() not in persisted
            assert memory_context_secret.encode() not in persisted
            assert SecretToolModel.final_answer.encode() not in persisted

    assert len(context_calls) == 2

    with open_encrypted_sqlite_checkpointer(database_path) as checkpointer:
        reopened = build_leon_graph(
            SecretToolModel(),
            _secret_registry(secret),
            checkpointer=checkpointer,
        )
        messages = reopened.get_state(config).values["messages"]

    tool_message = next(
        message for message in messages if isinstance(message, ToolMessage)
    )
    assert json.loads(tool_message.content)["content"] == secret
    assert messages[-1].content == SecretToolModel.final_answer
    assert all(memory_context_secret not in str(message.content) for message in messages)

    connection = sqlite3.connect(database_path)
    try:
        stored_types = {
            row[0]
            for table in ("checkpoints", "writes")
            for row in connection.execute(f"SELECT DISTINCT type FROM {table}")
        }
    finally:
        connection.close()
    assert stored_types
    assert all(value.endswith("+aes") for value in stored_types)


def test_checkpoint_key_is_created_once_and_never_stored_in_database(tmp_path) -> None:
    database_path = tmp_path / "checkpoints.db"
    key_path = checkpoint_key_path(database_path)

    with open_encrypted_sqlite_checkpointer(database_path) as checkpointer:
        checkpointer.get_tuple(
            {"configurable": {"thread_id": "lg-key-proof", "checkpoint_ns": ""}}
        )
    first_key = key_path.read_bytes()

    with open_encrypted_sqlite_checkpointer(database_path) as checkpointer:
        checkpointer.get_tuple(
            {"configurable": {"thread_id": "lg-key-proof", "checkpoint_ns": ""}}
        )

    assert len(first_key) == 32
    assert key_path.read_bytes() == first_key
    assert first_key not in database_path.read_bytes()


def test_invalid_checkpoint_key_fails_closed_without_overwrite(tmp_path) -> None:
    database_path = tmp_path / "checkpoints.db"
    key_path = checkpoint_key_path(database_path)
    invalid_key = b"too-short"
    key_path.write_bytes(invalid_key)

    with pytest.raises(CheckpointSecurityError, match="exactly 32 bytes"):
        with open_encrypted_sqlite_checkpointer(database_path):
            pass

    assert key_path.read_bytes() == invalid_key


def test_wrong_key_and_plaintext_database_fail_closed(tmp_path) -> None:
    encrypted_path = tmp_path / "encrypted.db"
    config = {"configurable": {"thread_id": "lg-key-check"}}
    with open_encrypted_sqlite_checkpointer(encrypted_path) as checkpointer:
        graph = build_leon_graph(
            SecretToolModel(),
            _secret_registry("secret"),
            checkpointer=checkpointer,
        )
        graph.invoke({"messages": [HumanMessage(content="read")]}, config)

    checkpoint_key_path(encrypted_path).write_bytes(b"1" * 32)
    with pytest.raises(CheckpointSecurityError, match="cannot be decrypted"):
        with open_encrypted_sqlite_checkpointer(encrypted_path):
            pass

    plaintext_path = tmp_path / "plaintext.db"
    connection = sqlite3.connect(plaintext_path, check_same_thread=False)
    try:
        checkpointer = SqliteSaver(connection)
        graph = build_leon_graph(
            SecretToolModel(),
            _secret_registry("legacy-secret"),
            checkpointer=checkpointer,
        )
        graph.invoke({"messages": [HumanMessage(content="legacy")]}, config)
    finally:
        connection.close()
    checkpoint_key_path(plaintext_path).write_bytes(b"2" * 32)

    with pytest.raises(CheckpointSecurityError, match="plaintext or unsupported"):
        with open_encrypted_sqlite_checkpointer(plaintext_path):
            pass


def test_interrupted_graph_resumes_after_reopen_without_repeating_tool(tmp_path) -> None:
    database_path = tmp_path / "resume.db"
    config = {"configurable": {"thread_id": create_thread_id()}}
    calls: list[dict[str, Any]] = []

    def read_file(**arguments: Any) -> dict[str, Any]:
        calls.append(arguments)
        return {"ok": True, "content": "cross-process evidence"}

    registry = ToolRegistry(
        [
            AgentTool(
                name="read_file",
                description="Read one fixture file.",
                parameters={
                    "type": "object",
                    "properties": {
                        "root_id": {"type": "string"},
                        "relative_path": {"type": "string"},
                    },
                    "required": ["root_id", "relative_path"],
                    "additionalProperties": False,
                },
                handler=read_file,
            )
        ]
    )

    with open_encrypted_sqlite_checkpointer(database_path) as checkpointer:
        first_process_graph = build_leon_graph(
            SecretToolModel(),
            registry,
            interrupt_before=["tools"],
            checkpointer=checkpointer,
        )
        list(
            first_process_graph.stream(
                {"messages": [HumanMessage(content="read fixture")]},
                config,
                stream_mode="updates",
            )
        )
        assert first_process_graph.get_state(config).next == ("tools",)
        assert calls == []

    with open_encrypted_sqlite_checkpointer(database_path) as checkpointer:
        second_process_graph = build_leon_graph(
            SecretToolModel(),
            registry,
            interrupt_before=["tools"],
            checkpointer=checkpointer,
        )
        list(second_process_graph.stream(None, config, stream_mode="updates"))
        assert second_process_graph.get_state(config).next == ()
        assert len(calls) == 1

    with open_encrypted_sqlite_checkpointer(database_path) as checkpointer:
        completed_graph = build_leon_graph(
            SecretToolModel(),
            registry,
            interrupt_before=["tools"],
            checkpointer=checkpointer,
        )
        assert completed_graph.get_state(config).next == ()
        assert len(calls) == 1
