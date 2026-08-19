from __future__ import annotations

from typing import Any

from leon_agent.evaluation.models import EvalCase
from leon_agent.evaluation.runner import _configure_utf8_output, load_cases, run_suite
from leon_agent.evaluation.scoring import score_case
from workbench_core.agent import AgentResult, ToolStep


class _ReconfigurableStream:
    def __init__(self) -> None:
        self.options: dict[str, Any] = {}

    def reconfigure(self, **kwargs: Any) -> None:
        self.options = kwargs


def test_evaluation_cli_configures_utf8_output(monkeypatch: Any) -> None:
    stdout = _ReconfigurableStream()
    stderr = _ReconfigurableStream()
    monkeypatch.setattr("leon_agent.evaluation.runner.sys.stdout", stdout)
    monkeypatch.setattr("leon_agent.evaluation.runner.sys.stderr", stderr)

    _configure_utf8_output()

    assert stdout.options == {"encoding": "utf-8", "errors": "replace"}
    assert stderr.options == {"encoding": "utf-8", "errors": "replace"}


def test_evaluation_dataset_has_fifty_three_unique_cases() -> None:
    cases = load_cases()

    assert len(cases) == 53
    assert len({case.id for case in cases}) == 53
    assert all(case.fake_turns for case in cases)


def test_fake_evaluation_suite_is_deterministic_and_side_effect_free() -> None:
    summary = run_suite(load_cases())

    assert summary.total_cases == 53
    assert summary.passed_cases == 53
    assert summary.rates.task_success == 1
    assert summary.rates.safety == 1
    assert summary.input_tokens == 7_095
    assert summary.output_tokens == 710


def test_scoring_reports_missing_tool_and_answer_assertion() -> None:
    case = EvalCase.model_validate(
        {
            "id": "scoring_contract_case",
            "category": "test",
            "user_message": "测试",
            "expectations": {
                "required_tools": ["file_search"],
                "answer_contains": ["citation"],
                "plan": {"mode": "forbidden"},
            },
            "fake_turns": [{"content": "完成"}],
        }
    )
    result = score_case(
        case,
        AgentResult(
            answer="没有引用",
            steps=[
                ToolStep(
                    name="web_search",
                    arguments={},
                    result={"ok": True},
                )
            ],
            turns=1,
        ),
        provider="fake",
        latency_ms=1.0,
    )

    assert result.passed is False
    assert result.metrics["tool_selection"].passed is False
    assert result.metrics["answer_quality"].passed is False


def test_scoring_accepts_one_answer_alternative_per_group() -> None:
    case = EvalCase.model_validate(
        {
            "id": "answer_alternatives_case",
            "category": "test",
            "user_message": "测试",
            "expectations": {
                "answer_contains_any": [["无法", "不能"], ["搜索", "网页"]],
            },
            "fake_turns": [{"content": "当前无法访问实时网页。"}],
        }
    )

    result = score_case(
        case,
        AgentResult(answer="当前无法访问实时网页。", steps=[], turns=1),
        provider="fake",
        latency_ms=1,
    )

    assert result.metrics["answer_quality"].passed is True


def test_scoring_names_tools_when_call_limit_is_exceeded() -> None:
    case = EvalCase.model_validate(
        {
            "id": "tool_limit_case",
            "category": "test",
            "user_message": "测试",
            "expectations": {"max_tool_calls": 0},
            "fake_turns": [{"content": "完成"}],
        }
    )
    result = score_case(
        case,
        AgentResult(
            answer="完成",
            steps=[
                ToolStep(name="list_image_modes", arguments={}, result={"ok": True}),
                ToolStep(
                    name="check_image_environment",
                    arguments={},
                    result={"ok": True},
                ),
            ],
            turns=1,
        ),
        provider="fake",
        latency_ms=1,
    )

    assert result.metrics["tool_selection"].details == [
        "tool calls 2 exceeded max 0: list_image_modes, check_image_environment"
    ]


def test_live_provider_requires_explicit_client() -> None:
    try:
        run_suite(load_cases()[:1], provider="live")
    except ValueError as exc:
        assert "explicit LLMClient" in str(exc)
    else:
        raise AssertionError("live evaluation must require an explicit client")


def test_tool_argument_assertion_uses_transient_transcript_without_echoing_values() -> None:
    case = EvalCase.model_validate(
        {
            "id": "argument_contract_case",
            "category": "test",
            "user_message": "测试",
            "expectations": {
                "required_tools": ["web_search"],
                "tool_arguments": [
                    {
                        "name": "web_search",
                        "arguments": {"query": "expected-private-query"},
                    }
                ],
            },
            "fake_turns": [{"content": "完成"}],
        }
    )
    result = score_case(
        case,
        AgentResult(
            answer="完成",
            steps=[ToolStep(name="web_search", arguments={}, result={"ok": True})],
            turns=1,
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query":"wrong-private-query"}',
                            }
                        }
                    ],
                }
            ],
        ),
        provider="fake",
        latency_ms=1,
    )

    assert result.metrics["tool_selection"].passed is False
    details = repr(result.metrics["tool_selection"].details)
    assert "expected-private-query" not in details
    assert "wrong-private-query" not in details
