"""CLI and orchestration for Leon Agent behavior evaluation."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter
from workbench_core.config import reset_settings_cache
from workbench_core.llm import ChatTurn, LLMClient, ToolCall

from leon_agent.config_file import apply_config_file
from leon_agent.evaluation.fixtures import create_eval_agent
from leon_agent.evaluation.models import (
    EvalCase,
    EvalCaseResult,
    EvalRates,
    EvalSummary,
    FakeTurn,
    MetricResult,
)
from leon_agent.evaluation.scoring import metric_rate, score_case

_DEFAULT_CASES_PATH = Path(__file__).resolve().parents[3] / "evals" / "cases"
_CASE_LIST = TypeAdapter(list[EvalCase])
_METRIC_NAMES = (
    "task_success",
    "tool_selection",
    "plan_adherence",
    "answer_quality",
    "safety",
)


def _configure_utf8_output() -> None:
    """Keep evaluator output readable when Windows defaults Python to GBK."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


class ScriptedEvalClient:
    """Replay case turns to validate the harness without contacting a provider."""

    def __init__(self, turns: Sequence[FakeTurn]) -> None:
        self._turns = list(turns)
        self._index = 0

    def _next_turn(self) -> FakeTurn:
        if self._index >= len(self._turns):
            raise RuntimeError("fake provider script was exhausted")
        turn = self._turns[self._index]
        self._index += 1
        return turn

    def chat_turn(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        cancel_event: Any = None,
        on_delta: Any = None,
    ) -> ChatTurn:
        del messages, temperature, cancel_event
        turn = self._next_turn()
        available_tools = {
            str(item.get("function", {}).get("name") or "") for item in tools or []
        }
        unknown_tools = [call.name for call in turn.tool_calls if call.name not in available_tools]
        if unknown_tools:
            raise RuntimeError(
                f"fake case called unavailable tools: {', '.join(unknown_tools)}"
            )
        if turn.content and on_delta is not None:
            on_delta(turn.content)
        calls: list[ToolCall] = []
        raw_calls: list[dict[str, Any]] = []
        for index, call in enumerate(turn.tool_calls, start=1):
            arguments = json.dumps(call.arguments, ensure_ascii=False)
            call_id = f"eval-call-{self._index}-{index}"
            calls.append(ToolCall(id=call_id, name=call.name, arguments=arguments))
            raw_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": arguments},
                }
            )
        raw_message: dict[str, Any] = {"role": "assistant", "content": turn.content}
        if raw_calls:
            raw_message["tool_calls"] = raw_calls
        return ChatTurn(
            content=turn.content,
            tool_calls=calls,
            raw_message=raw_message,
            usage={
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
            },
            model="eval-fake",
        )

    def chat(self, messages: Sequence[dict[str, Any]], **kwargs: Any) -> str:
        turn = self.chat_turn(messages, **kwargs)
        if turn.content is None:
            raise RuntimeError("closing fake turn did not contain an answer")
        return turn.content


def load_cases(path: str | Path | None = None) -> list[EvalCase]:
    resolved = Path(path) if path is not None else _DEFAULT_CASES_PATH
    paths = sorted(resolved.glob("*.json")) if resolved.is_dir() else [resolved]
    if not paths:
        raise ValueError(f"no evaluation case files found under {resolved}")
    cases: list[EvalCase] = []
    for case_path in paths:
        payload = json.loads(case_path.read_text(encoding="utf-8"))
        cases.extend(_CASE_LIST.validate_python(payload))
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("evaluation case ids must be unique")
    return cases


def _failed_result(
    case: EvalCase,
    *,
    provider: Literal["fake", "live"],
    latency_ms: float,
    error: Exception | str,
) -> EvalCaseResult:
    detail = str(error)
    metrics = {
        name: MetricResult(passed=False, details=[f"execution failed: {detail}"])
        for name in _METRIC_NAMES
    }
    return EvalCaseResult(
        case_id=case.id,
        category=case.category,
        provider=provider,
        passed=False,
        error=detail,
        latency_ms=latency_ms,
        turns=0,
        tool_calls=0,
        metrics=metrics,
    )


def _summary(
    provider: Literal["fake", "live"],
    results: list[EvalCaseResult],
    *,
    regressions: list[str] | None = None,
) -> EvalSummary:
    input_values = [item.input_tokens for item in results if item.input_tokens is not None]
    output_values = [item.output_tokens for item in results if item.output_tokens is not None]
    cost_values = [item.estimated_cost for item in results if item.estimated_cost is not None]
    return EvalSummary(
        provider=provider,
        total_cases=len(results),
        passed_cases=sum(item.passed for item in results),
        rates=EvalRates(
            task_success=metric_rate(results, "task_success"),
            tool_selection=metric_rate(results, "tool_selection"),
            plan_adherence=metric_rate(results, "plan_adherence"),
            answer_quality=metric_rate(results, "answer_quality"),
            safety=metric_rate(results, "safety"),
        ),
        total_latency_ms=sum(item.latency_ms for item in results),
        total_tool_calls=sum(item.tool_calls for item in results),
        input_tokens=sum(input_values) if input_values else None,
        output_tokens=sum(output_values) if output_values else None,
        estimated_cost=sum(cost_values) if cost_values else None,
        regressions=regressions or [],
        cases=results,
    )


def compare_with_baseline(current: EvalSummary, baseline: EvalSummary) -> list[str]:
    previous = {item.case_id: item for item in baseline.cases}
    regressions: list[str] = []
    for item in current.cases:
        old = previous.get(item.case_id)
        if old is None:
            continue
        if old.passed and not item.passed:
            regressions.append(f"{item.case_id}: task_success regressed")
            continue
        for name in _METRIC_NAMES[1:]:
            if old.metrics[name].passed and not item.metrics[name].passed:
                regressions.append(f"{item.case_id}: {name} regressed")
    return regressions


def run_suite(
    cases: Sequence[EvalCase],
    *,
    provider: Literal["fake", "live"] = "fake",
    live_client: LLMClient | None = None,
    input_cost_per_million: float | None = None,
    output_cost_per_million: float | None = None,
) -> EvalSummary:
    if provider == "live" and live_client is None:
        raise ValueError("live provider requires an explicit LLMClient")
    results: list[EvalCaseResult] = []
    with tempfile.TemporaryDirectory(prefix="leon-eval-") as temp_dir:
        suite_root = Path(temp_dir)
        for case in cases:
            started = time.perf_counter()
            try:
                if provider == "live" and not case.allow_live:
                    raise RuntimeError("case does not allow live provider execution")
                client = live_client if provider == "live" else ScriptedEvalClient(case.fake_turns)
                agent = create_eval_agent(case, client, suite_root / case.id)
                result = agent.run(case.user_message)
                latency_ms = (time.perf_counter() - started) * 1_000
                results.append(
                    score_case(
                        case,
                        result,
                        provider=provider,
                        latency_ms=latency_ms,
                        input_cost_per_million=input_cost_per_million,
                        output_cost_per_million=output_cost_per_million,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad case must not abort the suite
                results.append(
                    _failed_result(
                        case,
                        provider=provider,
                        latency_ms=(time.perf_counter() - started) * 1_000,
                        error=exc,
                    )
                )
    return _summary(provider, results)


def _print_summary(summary: EvalSummary) -> None:
    print(
        f"Leon Evaluation [{summary.provider}] "
        f"{summary.passed_cases}/{summary.total_cases} passed"
    )
    print(
        "rates: "
        f"task={summary.rates.task_success:.0%} "
        f"tools={summary.rates.tool_selection:.0%} "
        f"plan={summary.rates.plan_adherence:.0%} "
        f"answer={summary.rates.answer_quality:.0%} "
        f"safety={summary.rates.safety:.0%}"
    )
    print(
        f"latency_ms={summary.total_latency_ms:.1f} "
        f"tool_calls={summary.total_tool_calls} "
        f"tokens={summary.input_tokens or 0}/{summary.output_tokens or 0} "
        f"cost={summary.estimated_cost if summary.estimated_cost is not None else 'n/a'}"
    )
    for item in summary.cases:
        marker = "PASS" if item.passed else "FAIL"
        print(
            f"[{marker}] {item.case_id} "
            f"{item.latency_ms:.1f}ms tools={item.tool_calls} turns={item.turns}"
        )
        if not item.passed:
            for detail in item.metrics["task_success"].details:
                print(f"  - {detail}")
            if item.error:
                print(f"  - error: {item.error}")
    for regression in summary.regressions:
        print(f"[REGRESSION] {regression}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Leon Agent behavior evaluation.")
    parser.add_argument("--cases", type=Path, default=_DEFAULT_CASES_PATH)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Explicitly use the real provider from the Leon user config.",
    )
    parser.add_argument("--output", type=Path, help="Write the JSON result to this path.")
    parser.add_argument("--baseline", type=Path, help="Compare against a prior JSON result.")
    parser.add_argument("--input-cost-per-million", type=float)
    parser.add_argument("--output-cost-per-million", type=float)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_output()
    args = _parser().parse_args(argv)
    cases = load_cases(args.cases)
    if args.case_ids:
        requested = set(args.case_ids)
        cases = [case for case in cases if case.id in requested]
        missing = requested - {case.id for case in cases}
        if missing:
            raise SystemExit(f"unknown case ids: {', '.join(sorted(missing))}")

    provider: Literal["fake", "live"] = "live" if args.live else "fake"
    live_client: LLMClient | None = None
    if args.live:
        apply_config_file()
        reset_settings_cache()
        live_client = LLMClient()
    try:
        summary = run_suite(
            cases,
            provider=provider,
            live_client=live_client,
            input_cost_per_million=args.input_cost_per_million,
            output_cost_per_million=args.output_cost_per_million,
        )
    finally:
        if live_client is not None:
            live_client.close()

    if args.baseline:
        baseline = EvalSummary.model_validate_json(args.baseline.read_text(encoding="utf-8"))
        summary.regressions = compare_with_baseline(summary, baseline)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            summary.model_dump_json(indent=2),
            encoding="utf-8",
        )
    _print_summary(summary)
    return 0 if summary.passed_cases == summary.total_cases and not summary.regressions else 1


if __name__ == "__main__":
    raise SystemExit(main())
