from __future__ import annotations

from leon_framework.runtime_comparison import (
    comparison_cases,
    render_markdown,
    run_controlled_comparison,
)


def test_controlled_comparison_passes_all_shared_cases() -> None:
    report = run_controlled_comparison(repeats=1)

    assert len(comparison_cases()) == 10
    assert report.all_passed is True
    assert all(case.self_built_success for case in report.cases)
    assert all(case.langgraph_success for case in report.cases)
    assert all(case.observation_parity for case in report.cases)
    assert any(len(case.expected_tools) == 3 for case in report.cases)


def test_markdown_labels_timing_and_line_count_boundaries() -> None:
    report = run_controlled_comparison(repeats=1)

    markdown = render_markdown(report)

    assert "Self-built task success: 10/10" in markdown
    assert "LangGraph task success: 10/10" in markdown
    assert "Raw observation parity: 10/10" in markdown
    assert "do not represent provider latency" in markdown
    assert report.source_lines["self_runtime"] > report.source_lines["graph"]
