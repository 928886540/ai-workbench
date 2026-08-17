from __future__ import annotations

from leon_agent.evaluation.models import EvalCase
from leon_agent.evaluation.runner import load_cases, run_suite
from leon_agent.evaluation.scoring import score_case
from workbench_core.agent import AgentResult, ToolStep


def test_evaluation_dataset_has_twenty_unique_cases() -> None:
    cases = load_cases()

    assert len(cases) == 20
    assert len({case.id for case in cases}) == 20
    assert all(case.fake_turns for case in cases)


def test_fake_evaluation_suite_is_deterministic_and_side_effect_free() -> None:
    summary = run_suite(load_cases())

    assert summary.total_cases == 20
    assert summary.passed_cases == 20
    assert summary.rates.task_success == 1
    assert summary.rates.safety == 1
    assert summary.input_tokens == 6_640
    assert summary.output_tokens == 649


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


def test_live_provider_requires_explicit_client() -> None:
    try:
        run_suite(load_cases()[:1], provider="live")
    except ValueError as exc:
        assert "explicit LLMClient" in str(exc)
    else:
        raise AssertionError("live evaluation must require an explicit client")
