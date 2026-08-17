from __future__ import annotations

import json
import sys
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pytest
from coding_agent import CodingAgent
from workbench_core.agent import InMemoryTraceSink, TraceContext
from workbench_core.llm import ChatTurn, ToolCall


@dataclass(frozen=True)
class DemoCase:
    name: str
    task: str
    initial: str
    expected: str
    final: str
    actions: tuple[tuple[str, dict[str, Any]], ...]
    expected_test_results: tuple[bool, ...]


class ScriptedClient:
    model = "fake-coding-agent"

    def __init__(self, actions: tuple[tuple[str, dict[str, Any]], ...]) -> None:
        self._actions = list(actions)
        self.message_snapshots: list[list[dict[str, Any]]] = []
        self._call_id = 0

    def chat_turn(self, messages, **_kwargs):  # noqa: ANN001, ANN201
        self.message_snapshots.append(deepcopy(messages))
        if not self._actions:
            answer = "修改完成；测试结果与 Git diff 已核对。"
            return ChatTurn(
                content=answer,
                raw_message={"role": "assistant", "content": answer},
                usage={"input_tokens": 8, "output_tokens": 4},
                model=self.model,
            )
        name, arguments = self._actions.pop(0)
        self._call_id += 1
        call_id = f"call-{self._call_id}"
        encoded = json.dumps(arguments, ensure_ascii=False)
        return ChatTurn(
            content=None,
            tool_calls=[ToolCall(id=call_id, name=name, arguments=encoded)],
            raw_message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": encoded},
                    }
                ],
            },
            usage={"input_tokens": 8, "output_tokens": 4},
            model=self.model,
        )


CASES = (
    DemoCase(
        name="fix_bug",
        task="修复 add 把加法写成减法的问题。",
        initial="def add(left, right):\n    return left - right\n",
        expected="return left + right",
        final="def add(left, right):\n    return left + right\n",
        actions=(
            ("read_file", {"relative_path": "app.py"}),
            ("plan_create", {"steps": ["修复实现", "运行测试"]}),
            (
                "write_file",
                {
                    "relative_path": "app.py",
                    "content": "def add(left, right):\n    return left + right\n",
                },
            ),
            ("run_tests", {}),
            ("show_diff", {}),
        ),
        expected_test_results=(True,),
    ),
    DemoCase(
        name="small_feature",
        task="为工具模块增加 double 小功能。",
        initial="def identity(value):\n    return value\n",
        expected="def double(value):",
        final=(
            "def identity(value):\n    return value\n\n\n"
            "def double(value):\n    return value * 2\n"
        ),
        actions=(
            ("search_code", {"query": "identity"}),
            ("plan_create", {"steps": ["定位模块", "增加功能", "运行测试"]}),
            (
                "write_file",
                {
                    "relative_path": "app.py",
                    "content": (
                        "def identity(value):\n    return value\n\n\n"
                        "def double(value):\n    return value * 2\n"
                    ),
                },
            ),
            ("run_tests", {}),
            ("show_diff", {}),
        ),
        expected_test_results=(True,),
    ),
    DemoCase(
        name="repair_after_failure",
        task="修复 status，并根据第一次失败结果再修一次。",
        initial="def status():\n    return 'broken'\n",
        expected="return 'ok'",
        final="def status():\n    return 'ok'\n",
        actions=(
            ("read_file", {"relative_path": "app.py"}),
            ("plan_create", {"steps": ["修改实现", "运行测试", "按失败反馈修复"]}),
            (
                "write_file",
                {
                    "relative_path": "app.py",
                    "content": "def status():\n    return 'still-broken'\n",
                },
            ),
            ("run_tests", {}),
            (
                "write_file",
                {
                    "relative_path": "app.py",
                    "content": "def status():\n    return 'ok'\n",
                },
            ),
            ("run_tests", {}),
            ("show_diff", {}),
        ),
        expected_test_results=(False, True),
    ),
)


def _test_command(expected: str) -> tuple[str, ...]:
    script = (
        "from pathlib import Path; "
        "text=Path('app.py').read_text(encoding='utf-8'); "
        f"ok={expected!r} in text; "
        "print('tests passed' if ok else 'expected snippet missing'); "
        "raise SystemExit(0 if ok else 1)"
    )
    return sys.executable, "-c", script


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_three_stable_vertical_agent_cases(case: DemoCase, git_repo_factory) -> None:  # noqa: ANN001
    workspace = git_repo_factory({"app.py": case.initial})
    client = ScriptedClient(case.actions)
    sink = InMemoryTraceSink()
    context = TraceContext.create(session_id=case.name, entrypoint="eval")
    agent = CodingAgent(
        workspace,
        test_command=_test_command(case.expected),
        client=client,  # type: ignore[arg-type]
        authorize_write=lambda _request: True,
        authorize_test=lambda _request: True,
    )

    result = agent.run(case.task, trace_context=context, trace_sink=sink)

    assert (workspace.root / "app.py").read_text(encoding="utf-8") == case.final
    assert result.answer.startswith("修改完成")
    assert [step.name for step in result.steps] == [name for name, _ in case.actions]
    test_results = [
        step.result["passed"] for step in result.steps if step.name == "run_tests"
    ]
    assert tuple(test_results) == case.expected_test_results
    assert result.steps[-1].result["changed_count"] == 1

    trace = sink.traces[0]
    assert trace.status == "ok"
    assert trace.outcome == "answered"
    assert trace.planning_call_count == 1
    assert trace.tool_call_count == len(case.actions) - 1
    assert case.final not in repr((sink.traces, sink.spans))

    if case.name == "repair_after_failure":
        before_second_write = client.message_snapshots[4]
        assert "expected snippet missing" in repr(before_second_write)
