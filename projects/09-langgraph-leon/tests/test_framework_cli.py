from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage
from leon_framework import cli
from workbench_core.agent import ToolRegistry


class DirectModel:
    def invoke(self, messages: list[Any]) -> AIMessage:
        assert messages[-1].content == "分析项目"
        return AIMessage(content="framework-chat")


def test_demo_runs_without_provider(capsys) -> None:  # noqa: ANN001
    assert cli.main(["--demo"]) == 0

    output = capsys.readouterr().out
    assert "-> agent" in output
    assert "-> tools" in output
    assert "-> plan" in output
    assert "Locate the requested repository document." in output
    assert "Existing Leon read_file tool completed successfully." in output


def test_interrupt_demo_pauses_and_resumes_without_provider(capsys) -> None:  # noqa: ANN001
    assert cli.main(["--interrupt-demo"]) == 0

    output = capsys.readouterr().out
    assert "INTERRUPTED before tools" in output
    assert "RESUME" in output
    assert output.index("INTERRUPTED before tools") < output.index("  -> tools")
    assert output.count("  -> tools") == 1
    assert "Existing Leon read_file tool completed successfully." in output


def test_once_runs_minimal_live_path_with_fake_model(
    monkeypatch,
    capsys,
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

    result = cli.main(["--once", "分析项目", "--thread-id", "test-thread"])

    assert result == 0
    output = capsys.readouterr().out
    assert "Runtime   LangGraph" in output
    assert "Tools     chat only" in output
    assert "Planning  disabled" in output
    assert "Memory    enabled" in output
    assert "Thread    test-thread (in-memory checkpoint)" in output
    assert "leon > framework-chat" in output
    assert build_kwargs == {"session_id": "test-thread", "enable_memory": True}


def test_once_enables_planning_only_when_explicitly_requested(
    monkeypatch,
    capsys,
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

    result = cli.main(["--once", "分析项目", "--plan", "--no-memory"])

    assert result == 0
    output = capsys.readouterr().out
    assert "Planning  enabled (+1 model request/turn)" in output
    assert "Memory    disabled" in output
    assert "-> plan" in output
    assert "1. Inspect the request." in output
