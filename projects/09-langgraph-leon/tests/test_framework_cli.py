from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from leon_framework import cli
from workbench_core.agent import AgentTool, ToolRegistry


class DirectModel:
    def invoke(self, messages: list[Any]) -> AIMessage:
        assert messages[-1].content == "分析项目"
        return AIMessage(content="framework-chat")


class EchoModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, messages: list[Any]) -> AIMessage:
        prompt = str(messages[-1].content)
        self.prompts.append(prompt)
        return AIMessage(content=f"answer:{prompt}")


def test_demo_runs_without_provider(capsys) -> None:  # noqa: ANN001
    assert cli.main(["--demo"]) == 0

    output = capsys.readouterr().out
    assert "YOU  > 读取仓库 README 的前三行" in output
    assert "[GRAPH]  START" in output
    assert "[AGENT]  selected read_file" in output
    assert "[TOOL]   read_file" in output
    assert "[PLAN]   bounded steps" in output
    assert "[GRAPH]  DONE" in output
    assert "LEON >" in output
    assert "Locate the requested repository document." in output
    assert "Existing Leon read_file tool completed successfully." in output


def test_interrupt_demo_pauses_and_resumes_without_provider(
    tmp_path,
    capsys,
) -> None:  # noqa: ANN001
    database = tmp_path / "interrupt-demo.db"
    start_args = [
        "--interrupt-demo",
        "--thread-id",
        "demo-thread",
        "--checkpoint-db",
        str(database),
    ]
    assert cli.main(start_args) == 0

    paused = capsys.readouterr().out
    assert "[PAUSE]  before tools" in paused
    assert (
        "resume with: uv run --package leon-agent-framework leon-graph "
        "--interrupt-demo --resume demo-thread"
    ) in paused
    assert "[TOOL]" not in paused

    assert (
        cli.main(
            [
                "--interrupt-demo",
                "--resume",
                "demo-thread",
                "--checkpoint-db",
                str(database),
            ]
        )
        == 0
    )
    resumed = capsys.readouterr().out
    assert "[GRAPH]  RESUME" in resumed
    assert resumed.count("[TOOL]   read_file") == 1
    assert "[GRAPH]  DONE" in resumed
    assert "Existing Leon read_file tool completed successfully." in resumed

    assert (
        cli.main(
            [
                "--interrupt-demo",
                "--resume",
                "demo-thread",
                "--checkpoint-db",
                str(database),
            ]
        )
        == 2
    )
    assert "没有待恢复节点" in capsys.readouterr().err


def test_once_runs_minimal_live_path_with_fake_model(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:  # noqa: ANN001
    build_kwargs: dict[str, Any] = {}
    components = SimpleNamespace(
        model=DirectModel(),
        registry=ToolRegistry(),
        llm_settings=SimpleNamespace(active_model="fake-model"),
        memory_service=None,
    )

    def build_components(**kwargs: Any) -> SimpleNamespace:
        build_kwargs.update(kwargs)
        return components

    monkeypatch.setattr(cli, "build_framework_components", build_components)

    result = cli.main(
        [
            "--once",
            "分析项目",
            "--thread-id",
            "test-thread",
            "--checkpoint-db",
            str(tmp_path / "live.db"),
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "LEON AGENT FRAMEWORK EDITION" in output
    assert "RUNTIME    LangGraph" in output
    assert "TOOLS      chat only" in output
    assert "PLANNING   disabled" in output
    assert "MEMORY     enabled" in output
    assert "CHECKPOINT encrypted SQLite" in output
    assert "THREAD     test-thread" in output
    assert "YOU  > 分析项目" in output
    assert "[GRAPH]  START" in output
    assert "[AGENT]  model" in output
    assert "[GRAPH]  DONE" in output
    assert "LEON >\nframework-chat" in output
    assert output.index("YOU  >") < output.index("[GRAPH]  START")
    assert output.index("[GRAPH]  START") < output.index("LEON >")
    assert build_kwargs == {"session_id": "test-thread", "enable_memory": True}


def test_once_enables_planning_only_when_explicitly_requested(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:  # noqa: ANN001
    components = SimpleNamespace(
        model=DirectModel(),
        registry=ToolRegistry(),
        llm_settings=SimpleNamespace(active_model="fake-model"),
        memory_service=None,
    )
    monkeypatch.setattr(cli, "build_framework_components", lambda **kwargs: components)
    monkeypatch.setattr(
        cli,
        "build_model_planner",
        lambda model, names: lambda messages: [
            "Inspect the request.",
            "Answer from evidence.",
        ],
    )

    result = cli.main(
        [
            "--once",
            "分析项目",
            "--plan",
            "--no-memory",
            "--checkpoint-db",
            str(tmp_path / "plan.db"),
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "PLANNING   enabled (+1 model request/turn)" in output
    assert "MEMORY     disabled" in output
    assert "[PLAN]   bounded steps" in output
    assert "1. Inspect the request." in output


def test_live_resume_reuses_checkpoint_without_replaying_the_old_prompt(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:  # noqa: ANN001
    model = EchoModel()
    components = SimpleNamespace(
        model=model,
        registry=ToolRegistry(),
        llm_settings=SimpleNamespace(active_model="fake-model"),
        memory_service=None,
    )
    monkeypatch.setattr(cli, "build_framework_components", lambda **kwargs: components)
    database = tmp_path / "resume.db"

    assert (
        cli.main(
            [
                "--once",
                "第一问",
                "--thread-id",
                "resume-thread",
                "--checkpoint-db",
                str(database),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        cli.main(
            [
                "--resume",
                "resume-thread",
                "--once",
                "第二问",
                "--checkpoint-db",
                str(database),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "[RESUME] resume-thread" in output
    assert "LEON >\nanswer:第二问" in output
    assert model.prompts == ["第一问", "第二问"]


def test_new_thread_refuses_to_overwrite_existing_checkpoint(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:  # noqa: ANN001
    components = SimpleNamespace(
        model=DirectModel(),
        registry=ToolRegistry(),
        llm_settings=SimpleNamespace(active_model="fake-model"),
        memory_service=None,
    )
    monkeypatch.setattr(cli, "build_framework_components", lambda **kwargs: components)
    database = tmp_path / "collision.db"
    args = [
        "--once",
        "分析项目",
        "--thread-id",
        "collision-thread",
        "--checkpoint-db",
        str(database),
    ]
    assert cli.main(args) == 0
    capsys.readouterr()
    assert cli.main(args) == 2
    assert "thread 已存在" in capsys.readouterr().err


def test_interactive_resume_switches_to_existing_thread(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:  # noqa: ANN001
    components = SimpleNamespace(
        model=DirectModel(),
        registry=ToolRegistry(),
        llm_settings=SimpleNamespace(active_model="fake-model"),
        memory_service=None,
    )
    monkeypatch.setattr(cli, "build_framework_components", lambda **kwargs: components)
    database = tmp_path / "interactive-resume.db"
    assert (
        cli.main(
            [
                "--once",
                "分析项目",
                "--thread-id",
                "interactive-thread",
                "--checkpoint-db",
                str(database),
            ]
        )
        == 0
    )
    capsys.readouterr()
    inputs = iter(["/resume interactive-thread", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    assert cli.main(["--checkpoint-db", str(database)]) == 0
    output = capsys.readouterr().out
    assert "[RESUME] interactive-thread" in output


def test_resume_rejects_pending_write_tool() -> None:
    snapshot = SimpleNamespace(
        values={
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "memory_upsert",
                            "args": {"scope": "user", "key": "profile.x", "value": "secret"},
                            "id": "write-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        },
        next=("tools",),
    )

    registry = ToolRegistry(
        [
            AgentTool(
                name="memory_upsert",
                description="Write memory.",
                parameters={"type": "object"},
                handler=lambda **_: {"ok": True},
            )
        ]
    )
    with pytest.raises(cli.ResumeError, match="写入型工具"):
        cli._validate_pending_resume(
            snapshot,
            registry,
            planning_enabled=False,
        )


def test_resume_retries_plan_node_only_when_planning_is_enabled() -> None:
    snapshot = SimpleNamespace(
        values={"messages": [AIMessage(content="planner request pending")]},
        next=("plan",),
    )

    assert (
        cli._validate_pending_resume(
            snapshot,
            ToolRegistry(),
            planning_enabled=True,
        )
        is True
    )
    with pytest.raises(cli.ResumeError, match="带 --plan"):
        cli._validate_pending_resume(
            snapshot,
            ToolRegistry(),
            planning_enabled=False,
        )
