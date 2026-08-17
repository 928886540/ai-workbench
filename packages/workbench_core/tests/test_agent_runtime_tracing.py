import json
import logging
from dataclasses import replace
from threading import Event

import pytest
from workbench_core.agent import (
    AgentCancelled,
    AgentRuntime,
    AgentTool,
    InMemoryTraceSink,
    ToolRegistry,
    TraceContext,
)
from workbench_core.llm import ChatTurn, ToolCall


def _context() -> TraceContext:
    return TraceContext.create(session_id="session-1", entrypoint="eval")


class AnswerClient:
    model = "requested-model"

    def chat_turn(self, messages, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return ChatTurn(
            content="done",
            raw_message={"role": "assistant", "content": "done"},
            usage={"input_tokens": 4, "output_tokens": 2},
            model="served-model",
        )


class ToolThenAnswerClient:
    model = "requested-model"

    def __init__(self, *, tool_name: str = "plan_create", arguments: str | None = None) -> None:
        self.calls = 0
        self.tool_name = tool_name
        self.arguments = arguments or json.dumps({"steps": ["first", "second"]})

    def chat_turn(self, messages, **kwargs):  # noqa: ANN001, ANN201, ARG002
        self.calls += 1
        if self.calls == 1:
            return ChatTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name=self.tool_name,
                        arguments=self.arguments,
                    )
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [],
                },
                usage={"input_tokens": 5, "output_tokens": 1},
                model="served-model",
            )
        return ChatTurn(
            content="done",
            raw_message={"role": "assistant", "content": "done"},
            usage={"input_tokens": 3, "output_tokens": 2},
            model="served-model",
        )


def test_runtime_trace_records_answer_tree_usage_and_event_ids() -> None:
    sink = InMemoryTraceSink()
    context = _context()
    events = []
    runtime = AgentRuntime(
        client=AnswerClient(),  # type: ignore[arg-type]
        tools=ToolRegistry(),
        system_prompt="answer",
        on_event=events.append,
    )

    result = runtime.run("question", trace_context=context, trace_sink=sink)

    assert result.trace_id == context.trace_id
    assert result.turn_id == context.turn_id
    trace = sink.traces[0]
    assert trace.status == "ok"
    assert trace.outcome == "answered"
    assert trace.model == "served-model"
    assert trace.input_tokens == 4
    assert trace.output_tokens == 2
    assert trace.llm_call_count == 1
    spans = sink.spans
    assert [span.kind for span in spans] == ["agent", "iteration", "llm"]
    assert spans[1].parent_span_id == spans[0].span_id
    assert spans[2].parent_span_id == spans[1].span_id
    assert all(span.status == "ok" for span in spans)
    assert all(event.trace_id == context.trace_id for event in events)
    assert all(event.turn_id == context.turn_id for event in events)


def test_runtime_trace_records_planning_span_and_audit_correlation() -> None:
    sink = InMemoryTraceSink()
    context = _context()
    events = []
    registry = ToolRegistry(
        [
            AgentTool(
                name="plan_create",
                description="create plan",
                parameters={"type": "object"},
                handler=lambda steps: {"ok": True, "step_count": len(steps)},
                audit_arguments=lambda arguments: {"step_count": len(arguments["steps"])},
                audit_result=lambda result: {"ok": result["ok"]},
                span_kind="planning",
            )
        ]
    )
    runtime = AgentRuntime(
        client=ToolThenAnswerClient(),  # type: ignore[arg-type]
        tools=registry,
        system_prompt="plan",
        on_event=events.append,
    )

    result = runtime.run("plan it", trace_context=context, trace_sink=sink)

    trace = sink.traces[0]
    assert trace.llm_call_count == 2
    assert trace.tool_call_count == 0
    assert trace.planning_call_count == 1
    assert trace.input_tokens == 8
    assert trace.output_tokens == 3
    assert [span.kind for span in sink.spans] == [
        "agent",
        "iteration",
        "llm",
        "planning",
        "iteration",
        "llm",
    ]
    planning = next(span for span in sink.spans if span.kind == "planning")
    assert planning.name == "planning.create"
    assert planning.tool_name == "plan_create"
    assert result.steps[0].trace_id == context.trace_id
    assert result.steps[0].span_id == planning.span_id
    tool_events = [event for event in events if event.kind.startswith("tool_")]
    assert [event.span_id for event in tool_events] == [planning.span_id, planning.span_id]


def test_runtime_trace_falls_back_to_tool_for_legacy_executor() -> None:
    class LegacyExecutor:
        @property
        def schemas(self) -> list[dict[str, object]]:
            return []

        def execute(self, name, arguments):  # noqa: ANN001, ANN201
            return {"ok": True, "name": name, **arguments}

    sink = InMemoryTraceSink()
    runtime = AgentRuntime(
        client=ToolThenAnswerClient(
            tool_name="echo",
            arguments=json.dumps({"value": "hello"}),
        ),  # type: ignore[arg-type]
        tools=LegacyExecutor(),  # type: ignore[arg-type]
        system_prompt="tools",
    )

    runtime.run("echo", trace_context=_context(), trace_sink=sink)

    tool_span = next(span for span in sink.spans if span.kind == "tool")
    assert tool_span.name == "tool.call"
    assert tool_span.tool_name == "echo"


def test_runtime_trace_keeps_completed_tool_when_cancelled_after_handler() -> None:
    cancel_event = Event()
    sink = InMemoryTraceSink()

    def perform_side_effect(value: str) -> dict[str, object]:
        cancel_event.set()
        return {"ok": True, "value": value}

    runtime = AgentRuntime(
        client=ToolThenAnswerClient(
            tool_name="write",
            arguments=json.dumps({"value": "saved"}),
        ),  # type: ignore[arg-type]
        tools=ToolRegistry(
            [
                AgentTool(
                    name="write",
                    description="write",
                    parameters={"type": "object"},
                    handler=perform_side_effect,
                    audit_arguments=lambda arguments: {"authorized": bool(arguments)},
                    audit_result=lambda result: {"ok": result["ok"]},
                )
            ]
        ),
        system_prompt="tools",
    )

    with pytest.raises(AgentCancelled) as captured:
        runtime.run(
            "write",
            cancel_event=cancel_event,
            trace_context=_context(),
            trace_sink=sink,
        )

    trace = sink.traces[0]
    assert trace.status == "cancelled"
    assert trace.outcome == "cancelled"
    tool_span = next(span for span in sink.spans if span.kind == "tool")
    assert tool_span.status == "ok"
    assert next(span for span in sink.spans if span.kind == "iteration").status == "cancelled"
    partial = captured.value.partial_result
    assert partial is not None
    assert partial.steps[0].span_id == tool_span.span_id


def test_runtime_trace_marks_llm_failure_without_retaining_error_text() -> None:
    marker = "private-provider-response"

    class FailingClient:
        model = "requested-model"

        def chat_turn(self, messages, **kwargs):  # noqa: ANN001, ANN201, ARG002
            raise RuntimeError(marker)

    sink = InMemoryTraceSink()
    runtime = AgentRuntime(
        client=FailingClient(),  # type: ignore[arg-type]
        tools=ToolRegistry(),
        system_prompt="answer",
    )

    with pytest.raises(RuntimeError, match=marker):
        runtime.run("question", trace_context=_context(), trace_sink=sink)

    trace = sink.traces[0]
    assert trace.status == "error"
    assert trace.outcome == "failed"
    assert trace.error_type == "RuntimeError"
    assert all(span.status == "error" for span in sink.spans)
    assert marker not in repr((sink.traces, sink.spans))


def test_runtime_trace_completed_callback_cancellation_wins() -> None:
    cancel_event = Event()
    sink = InMemoryTraceSink()

    def cancel_on_completed(event) -> None:  # noqa: ANN001
        if event.kind == "completed":
            cancel_event.set()

    runtime = AgentRuntime(
        client=AnswerClient(),  # type: ignore[arg-type]
        tools=ToolRegistry(),
        system_prompt="answer",
        on_event=cancel_on_completed,
    )

    with pytest.raises(AgentCancelled):
        runtime.run(
            "question",
            cancel_event=cancel_event,
            trace_context=_context(),
            trace_sink=sink,
        )

    assert sink.traces[0].status == "cancelled"
    assert next(span for span in sink.spans if span.kind == "llm").status == "ok"
    assert next(span for span in sink.spans if span.kind == "iteration").status == "cancelled"


def test_runtime_trace_closing_span_does_not_invent_usage() -> None:
    class ClosingClient(ToolThenAnswerClient):
        def chat(self, messages, **kwargs):  # noqa: ANN001, ANN201, ARG002
            return "forced"

    sink = InMemoryTraceSink()
    runtime = AgentRuntime(
        client=ClosingClient(
            tool_name="echo",
            arguments=json.dumps({"value": "hello"}),
        ),  # type: ignore[arg-type]
        tools=ToolRegistry(
            [
                AgentTool(
                    name="echo",
                    description="echo",
                    parameters={"type": "object"},
                    handler=lambda value: {"ok": True, "value": value},
                )
            ]
        ),
        system_prompt="tools",
        max_turns=1,
    )

    result = runtime.run("echo", trace_context=_context(), trace_sink=sink)

    trace = sink.traces[0]
    assert result.answer == "forced"
    assert trace.outcome == "forced_answer"
    assert trace.llm_call_count == 2
    assert trace.input_tokens is None
    assert trace.output_tokens is None
    closing = [span for span in sink.spans if span.kind == "llm"][-1]
    assert closing.attributes["closing"] is True
    assert closing.input_tokens is None
    assert closing.output_tokens is None


@pytest.mark.parametrize(
    "failing_method",
    ["start_trace", "start_span", "finish_span", "finish_trace"],
)
def test_runtime_result_is_unchanged_when_trace_sink_fails(
    failing_method: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "private-sink-payload"

    class RaisingSink:
        def _call(self, method: str) -> None:
            if method == failing_method:
                raise RuntimeError(marker)

        def start_trace(self, trace) -> None:  # noqa: ANN001
            self._call("start_trace")

        def start_span(self, span) -> None:  # noqa: ANN001
            self._call("start_span")

        def finish_span(self, span) -> None:  # noqa: ANN001
            self._call("finish_span")

        def finish_trace(self, trace) -> None:  # noqa: ANN001
            self._call("finish_trace")

    caplog.set_level(logging.WARNING)
    baseline = AgentRuntime(
        client=AnswerClient(),  # type: ignore[arg-type]
        tools=ToolRegistry(),
        system_prompt="answer",
    ).run("question")
    traced = AgentRuntime(
        client=AnswerClient(),  # type: ignore[arg-type]
        tools=ToolRegistry(),
        system_prompt="answer",
    ).run("question", trace_context=_context(), trace_sink=RaisingSink())

    normalized = replace(traced, trace_id=None, turn_id=None)
    assert normalized == baseline
    assert marker not in caplog.text
