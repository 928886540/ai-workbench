from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leon_agent.agent import LeonAgent
from leon_agent.file_tools import create_file_search_service
from leon_agent.file_write_policy import create_file_write_service
from workbench_core.llm import ChatTurn, ToolCall


class FakeImageClient:
    def list_modes(self) -> dict[str, Any]:
        return {"ok": True, "modes": []}


class FileWriteClient:
    def __init__(self, *, relative_path: str, content: str) -> None:
        self.relative_path = relative_path
        self.content = content
        self.calls = 0
        self.tool_names: list[str] = []

    def chat_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> ChatTurn:
        del messages, temperature
        self.calls += 1
        self.tool_names = [item["function"]["name"] for item in tools or []]
        if self.calls == 1:
            arguments = json.dumps(
                {
                    "root_id": "docs",
                    "relative_path": self.relative_path,
                    "content": self.content,
                }
            )
            return ChatTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="create-call",
                        name="create_file",
                        arguments=arguments,
                    )
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "create-call",
                            "type": "function",
                            "function": {
                                "name": "create_file",
                                "arguments": arguments,
                            },
                        }
                    ],
                },
            )
        return ChatTurn(
            content="文件已创建。",
            raw_message={"role": "assistant", "content": "文件已创建。"},
        )


def _agent(tmp_path: Path, client: FileWriteClient) -> LeonAgent:
    return LeonAgent(
        llm_client=client,  # type: ignore[arg-type]
        image_client=FakeImageClient(),  # type: ignore[arg-type]
        session_id="test-session",
        default_mode_ids=["default"],
        file_service=create_file_search_service({"docs": tmp_path}),
        file_write_service=create_file_write_service({"docs": tmp_path}),
    )


def test_agent_advertises_write_prompt_only_when_write_tools_are_registered(
    tmp_path: Path,
) -> None:
    writable = _agent(
        tmp_path,
        FileWriteClient(relative_path="notes.md", content="value"),
    )
    read_only = LeonAgent(
        llm_client=FileWriteClient(  # type: ignore[arg-type]
            relative_path="notes.md",
            content="value",
        ),
        image_client=FakeImageClient(),  # type: ignore[arg-type]
        session_id="read-only-session",
        default_mode_ids=["default"],
        file_service=create_file_search_service({"docs": tmp_path}),
    )

    assert {"create_file", "write_file"}.issubset(set(writable.runtime.tools.names))
    assert "!file create" in writable.runtime.system_prompt
    assert writable.file_write_service is not None
    assert {"create_file", "write_file"}.isdisjoint(read_only.runtime.tools.names)
    assert "!file create" not in read_only.runtime.system_prompt
    assert read_only.file_write_service is None


def test_agent_executes_one_explicit_create_and_redacts_content(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    marker = "private-draft-content-marker"
    client = FileWriteClient(relative_path="notes/plan.md", content=marker)
    agent = _agent(tmp_path, client)

    result = agent.run("!file create docs:notes/plan.md")

    assert result.answer == "文件已创建。"
    assert (tmp_path / "notes" / "plan.md").read_text(encoding="utf-8") == marker
    assert {"list_files", "file_search", "read_file", "create_file", "write_file"}.issubset(
        client.tool_names
    )
    assert result.steps[0].arguments == {
        "root_id": "docs",
        "relative_path": "notes/plan.md",
    }
    assert marker not in repr(result.steps)


def test_agent_denies_tool_call_without_current_turn_authorization(tmp_path: Path) -> None:
    client = FileWriteClient(relative_path="notes.md", content="blocked")
    agent = _agent(tmp_path, client)

    result = agent.run("Leon 有 create_file 能力吗？")

    assert not (tmp_path / "notes.md").exists()
    assert result.steps[0].result == {
        "ok": False,
        "error_code": "authorization_required",
    }
