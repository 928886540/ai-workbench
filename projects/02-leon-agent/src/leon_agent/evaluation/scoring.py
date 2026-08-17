"""Deterministic scoring over one completed Agent result."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from workbench_core.agent import AgentResult, ToolStep

from leon_agent.evaluation.models import EvalCase, EvalCaseResult, MetricResult

_PLANNING_TOOLS = frozenset({"plan_create", "plan_update", "plan_get"})


def _contains(haystack: str, needle: str) -> bool:
    return needle.casefold() in haystack.casefold()


def _raw_tool_calls(result: AgentResult) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    for message in result.messages:
        if message.get("role") != "assistant":
            continue
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for raw_call in raw_calls:
            if not isinstance(raw_call, Mapping):
                continue
            function = raw_call.get("function")
            if not isinstance(function, Mapping):
                continue
            name = function.get("name")
            encoded = function.get("arguments")
            if not isinstance(name, str) or not isinstance(encoded, str):
                continue
            try:
                arguments = json.loads(encoded)
            except json.JSONDecodeError:
                continue
            if isinstance(arguments, dict):
                calls.append((name, arguments))
    return calls


def _matches_subset(expected: object, actual: object) -> bool:
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _matches_subset(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            _matches_subset(expected_item, actual_item)
            for expected_item, actual_item in zip(expected, actual, strict=True)
        )
    return expected == actual


def _tool_selection(case: EvalCase, result: AgentResult) -> MetricResult:
    steps = result.steps
    names = [step.name for step in steps]
    missing = [name for name in case.expectations.required_tools if name not in names]
    forbidden = [name for name in case.expectations.forbidden_tools if name in names]
    details: list[str] = []
    if missing:
        details.append(f"missing required tools: {', '.join(missing)}")
    if forbidden:
        details.append(f"used forbidden tools: {', '.join(forbidden)}")
    limit = case.expectations.max_tool_calls
    if limit is not None and len(steps) > limit:
        details.append(
            f"tool calls {len(steps)} exceeded max {limit}: {', '.join(names)}"
        )
    raw_calls = _raw_tool_calls(result)
    for expectation in case.expectations.tool_arguments:
        candidates = [arguments for name, arguments in raw_calls if name == expectation.name]
        if len(candidates) < expectation.occurrence:
            details.append(
                f"missing {expectation.name} occurrence {expectation.occurrence} "
                "for argument assertion"
            )
            continue
        actual = candidates[expectation.occurrence - 1]
        if not _matches_subset(expectation.arguments, actual):
            details.append(
                f"{expectation.name} occurrence {expectation.occurrence} "
                "did not satisfy argument assertion"
            )
    return MetricResult(passed=not details, details=details)


def _plan_adherence(case: EvalCase, steps: list[ToolStep]) -> MetricResult:
    expectation = case.expectations.plan
    plan_steps = [step for step in steps if step.name in _PLANNING_TOOLS]
    details: list[str] = []

    if expectation.mode == "forbidden":
        if plan_steps:
            details.append("planning was used for a case that forbids it")
        return MetricResult(passed=not details, details=details)
    if expectation.mode == "required" and not plan_steps:
        return MetricResult(passed=False, details=["required plan was not created"])
    if not plan_steps:
        return MetricResult(passed=True)

    create_indexes = [index for index, step in enumerate(steps) if step.name == "plan_create"]
    if len(create_indexes) != 1:
        details.append(f"expected one plan_create, got {len(create_indexes)}")
    else:
        first_domain_index = next(
            (index for index, step in enumerate(steps) if step.name not in _PLANNING_TOOLS),
            None,
        )
        if first_domain_index is not None and create_indexes[0] > first_domain_index:
            details.append("plan_create happened after the first domain tool")
        create_result = steps[create_indexes[0]].result
        step_count = create_result.get("step_count")
        if not isinstance(step_count, int) or not (
            expectation.min_steps <= step_count <= expectation.max_steps
        ):
            details.append(
                f"plan step count {step_count!r} outside "
                f"{expectation.min_steps}..{expectation.max_steps}"
            )

    failed_calls = [
        step.name for step in plan_steps if step.result.get("ok") is not True
    ]
    if failed_calls:
        details.append(f"planning tool failures: {', '.join(failed_calls)}")
    if not any(step.result.get("done") is True for step in reversed(plan_steps)):
        details.append("plan did not reach a terminal state")
    return MetricResult(passed=not details, details=details)


def _answer_quality(case: EvalCase, answer: str) -> MetricResult:
    details: list[str] = []
    missing = [
        value for value in case.expectations.answer_contains if not _contains(answer, value)
    ]
    forbidden = [
        value for value in case.expectations.answer_not_contains if _contains(answer, value)
    ]
    missing_alternatives = [
        group
        for group in case.expectations.answer_contains_any
        if not any(_contains(answer, value) for value in group)
    ]
    if missing:
        details.append(f"answer missing: {', '.join(missing)}")
    if missing_alternatives:
        formatted = [" | ".join(group) for group in missing_alternatives]
        details.append(f"answer missing alternatives: {'; '.join(formatted)}")
    if forbidden:
        details.append(f"answer contained forbidden text: {', '.join(forbidden)}")
    return MetricResult(passed=not details, details=details)


def _safety(case: EvalCase, result: AgentResult) -> MetricResult:
    audit_payload = json.dumps(
        [
            {"name": step.name, "arguments": step.arguments, "result": step.result}
            for step in result.steps
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    leaked = [
        value
        for value in case.expectations.audit_not_contains
        if _contains(audit_payload, value)
    ]
    details = [f"audit leaked forbidden text: {', '.join(leaked)}"] if leaked else []
    return MetricResult(passed=not details, details=details)


def estimate_cost(
    usage: dict[str, int] | None,
    *,
    input_cost_per_million: float | None,
    output_cost_per_million: float | None,
) -> float | None:
    if usage is None or input_cost_per_million is None or output_cost_per_million is None:
        return None
    return (
        usage.get("input_tokens", 0) * input_cost_per_million
        + usage.get("output_tokens", 0) * output_cost_per_million
    ) / 1_000_000


def score_case(
    case: EvalCase,
    result: AgentResult,
    *,
    provider: str,
    latency_ms: float,
    input_cost_per_million: float | None = None,
    output_cost_per_million: float | None = None,
) -> EvalCaseResult:
    metrics = {
        "tool_selection": _tool_selection(case, result),
        "plan_adherence": _plan_adherence(case, result.steps),
        "answer_quality": _answer_quality(case, result.answer),
        "safety": _safety(case, result),
    }
    task_success = MetricResult(
        passed=all(metric.passed for metric in metrics.values()),
        details=[detail for metric in metrics.values() for detail in metric.details],
    )
    metrics = {"task_success": task_success, **metrics}
    usage = result.usage or None
    return EvalCaseResult(
        case_id=case.id,
        category=case.category,
        provider=provider,
        passed=task_success.passed,
        answer=result.answer,
        latency_ms=latency_ms,
        turns=result.turns,
        tool_calls=len(result.steps),
        input_tokens=usage.get("input_tokens") if usage else None,
        output_tokens=usage.get("output_tokens") if usage else None,
        estimated_cost=estimate_cost(
            usage,
            input_cost_per_million=input_cost_per_million,
            output_cost_per_million=output_cost_per_million,
        ),
        metrics=metrics,
    )


def metric_rate(results: Iterable[EvalCaseResult], name: str) -> float:
    items = list(results)
    if not items:
        return 0.0
    return sum(item.metrics[name].passed for item in items) / len(items)
