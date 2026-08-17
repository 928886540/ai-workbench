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
        task="为工具模块增加 double 小功能并补测试。",
        initial=(
            "import unittest\n\n\n"
            "def identity(value):\n"
            "    return value\n\n\n"
            "class FeatureTests(unittest.TestCase):\n"
            "    def test_identity(self):\n"
            "        self.assertEqual(identity('x'), 'x')\n"
        ),
        expected="def double(value):",
        final=(
            "import unittest\n\n\n"
            "def identity(value):\n"
            "    return value\n\n\n"
            "def double(value):\n"
            "    return value * 2\n\n\n"
            "class FeatureTests(unittest.TestCase):\n"
            "    def test_identity(self):\n"
            "        self.assertEqual(identity('x'), 'x')\n\n"
            "    def test_double(self):\n"
            "        self.assertEqual(double(3), 6)\n"
        ),
        actions=(
            ("search_code", {"query": "identity"}),
            ("plan_create", {"steps": ["定位模块", "增加功能", "运行测试"]}),
            (
                "write_file",
                {
                    "relative_path": "app.py",
                    "content": (
                        "import unittest\n\n\n"
                        "def identity(value):\n"
                        "    return value\n\n\n"
                        "def double(value):\n"
                        "    return value * 2\n\n\n"
                        "class FeatureTests(unittest.TestCase):\n"
                        "    def test_identity(self):\n"
                        "        self.assertEqual(identity('x'), 'x')\n\n"
                        "    def test_double(self):\n"
                        "        self.assertEqual(double(3), 6)\n"
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


def _test_command(case: DemoCase) -> tuple[str, ...]:
    if case.name == "small_feature":
        return sys.executable, "-m", "unittest", "-q", "app.py"
    script = (
        "from pathlib import Path; "
        "text=Path('app.py').read_text(encoding='utf-8'); "
        f"ok={case.expected!r} in text; "
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
        test_command=_test_command(case),
        client=client,  # type: ignore[arg-type]
        authorize_write=lambda _request: True,
        authorize_test=lambda _request: True,
    )

    result = agent.run(case.task, trace_context=context, trace_sink=sink)

    assert (workspace.root / "app.py").read_text(encoding="utf-8") == case.final
    assert result.answer.startswith("修改完成")
    assert [step.name for step in result.steps] == [name for name, _ in case.actions]
    test_results = [step.result["passed"] for step in result.steps if step.name == "run_tests"]
    assert tuple(test_results) == case.expected_test_results
    assert result.steps[-1].result["changed_count"] == 1

    trace = sink.traces[0]
    spans = sink.spans
    assert trace.status == "ok"
    assert trace.outcome == "answered"
    assert trace.trace_id == context.trace_id
    assert trace.turn_id == context.turn_id
    assert trace.planning_call_count == 1
    assert trace.tool_call_count == len(case.actions) - 1
    assert spans[0].kind == "agent"
    assert spans[0].span_id == trace.root_span_id
    assert spans[0].parent_span_id is None

    action_spans = [span for span in spans if span.kind in {"planning", "tool"}]
    iteration_ids = {span.span_id for span in spans if span.kind == "iteration"}
    assert [span.tool_name for span in action_spans] == [name for name, _ in case.actions]
    assert [step.span_id for step in result.steps] == [span.span_id for span in action_spans]
    assert all(step.trace_id == context.trace_id for step in result.steps)
    assert all(span.parent_span_id in iteration_ids for span in action_spans)

    write_steps = [step for step in result.steps if step.name == "write_file"]
    assert all(step.result["authorized"] is True for step in write_steps)
    assert all("content" not in step.arguments for step in write_steps)
    assert all("query" not in step.arguments for step in result.steps)
    assert all("output" not in step.result for step in result.steps)
    assert all("diff" not in step.result for step in result.steps)

    trace_repr = repr((sink.traces, spans))
    assert case.task not in trace_repr
    assert case.initial not in trace_repr
    assert case.final not in trace_repr
    assert str(workspace.root) not in trace_repr
    assert workspace.root.as_posix() not in trace_repr

    if case.name == "repair_after_failure":
        before_second_write = client.message_snapshots[4]
        assert "expected snippet missing" in repr(before_second_write)
        test_steps = [step for step in result.steps if step.name == "run_tests"]
        test_spans = [span for span in action_spans if span.tool_name == "run_tests"]
        assert [step.result["passed"] for step in test_steps] == [False, True]
        assert [step.result["attempt"] for step in test_steps] == [1, 2]
        assert [step.span_id for step in test_steps] == [span.span_id for span in test_spans]
