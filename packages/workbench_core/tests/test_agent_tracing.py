import logging
from dataclasses import replace

import pytest
from workbench_core.agent.tracing import (
    InMemoryTraceSink,
    NoOpTraceSink,
    SpanRecord,
    TraceContext,
    TraceRecorder,
    project_trace_attributes,
)


class SequenceClock:
    def __init__(self, *values: int | float) -> None:
        self._values = iter(values)

    def __call__(self) -> int | float:
        return next(self._values)


def _context() -> TraceContext:
    return TraceContext(
        trace_id="1" * 32,
        turn_id="2" * 32,
        session_id=None,
        entrypoint="eval",
    )


def test_trace_context_create_generates_valid_ids_and_validates_input() -> None:
    context = TraceContext.create(session_id="session-1", entrypoint="cli")

    assert len(context.trace_id) == 32
    assert len(context.turn_id) == 32
    assert set(context.trace_id) <= set("0123456789abcdef")
    assert set(context.turn_id) <= set("0123456789abcdef")

    with pytest.raises(ValueError, match="trace_id"):
        replace(context, trace_id="ABC")
    with pytest.raises(ValueError, match="trace_id"):
        TraceContext.create(trace_id="")
    with pytest.raises(ValueError, match="session_id"):
        replace(context, session_id="secret\nvalue")


def test_span_ids_and_terminal_model_validation_are_strict() -> None:
    sink = InMemoryTraceSink()
    recorder = TraceRecorder(_context(), sink)
    span_id = recorder.start_span("llm", "llm.request")
    finished = recorder.finish_span(span_id, model="model-a")
    recorder.finish_trace(status="ok", outcome="answered")

    assert finished is not None
    assert len(recorder.root_span_id) == 16
    assert len(span_id) == 16
    assert set(recorder.root_span_id + span_id) <= set("0123456789abcdef")
    with pytest.raises(ValueError, match="span_id"):
        replace(finished, span_id="F" * 16)
    with pytest.raises(ValueError, match="duration_ms"):
        replace(finished, duration_ms=float("nan"))


def test_attribute_projection_is_default_deny_and_never_keeps_unsafe_text() -> None:
    marker = "private-prompt-marker"

    projected = project_trace_attributes(
        {
            "iteration": 2,
            "streaming": True,
            "unknown_prompt": marker,
            "requested_model": "model-a",
        }
    )
    invalid = project_trace_attributes({"iteration": {"secret": marker}})
    overlong = project_trace_attributes(
        {"requested_model": marker * 100, "message_count": 3}
    )

    assert projected == {
        "iteration": 2,
        "streaming": True,
        "requested_model": "model-a",
    }
    assert invalid == {"redaction_failed": True}
    assert overlong == {"message_count": 3, "truncated": True}
    assert marker not in repr(projected)
    assert marker not in repr(invalid)
    assert marker not in repr(overlong)
    assert project_trace_attributes({"truncated": marker}) == {
        "redaction_failed": True
    }


def test_unsafe_bounded_fields_fail_closed_without_repr_payload() -> None:
    marker = "private-runtime-payload"
    unsafe = marker * 20
    sink = InMemoryTraceSink()
    recorder = TraceRecorder(_context(), sink)
    span_id = recorder.start_span(
        "tool",
        unsafe,
        tool_name=unsafe,
        attributes={"requested_model": unsafe},
    )

    finished = recorder.finish_span(
        span_id,
        status="error",
        error_type=unsafe,
        model=unsafe,
    )

    assert finished is not None
    assert marker not in repr(finished)
    assert finished.name == "redaction_failed"
    assert finished.tool_name == "redaction_failed"
    assert finished.error_type == "redaction_failed"
    assert finished.model == "redaction_failed"
    assert finished.attributes == {"truncated": True}


def test_recorder_builds_stable_tree_and_aggregates_finished_spans() -> None:
    sink = InMemoryTraceSink()
    wall = SequenceClock(*(1000 + offset * 10 for offset in range(10)))
    monotonic = SequenceClock(*(10.0 + offset * 10 for offset in range(10)))
    recorder = TraceRecorder(
        _context(),
        sink,
        wall_clock_ms=wall,  # type: ignore[arg-type]
        monotonic_ms=monotonic,  # type: ignore[arg-type]
    )

    iteration_id = recorder.start_span("iteration", "agent.iteration")
    llm_id = recorder.start_span(
        "llm",
        "llm.request",
        parent_span_id=iteration_id,
        attributes={"iteration": 1, "message_count": 2},
    )
    recorder.finish_span(
        llm_id,
        model="served-model",
        input_tokens=10,
        output_tokens=5,
    )
    tool_id = recorder.start_span(
        "tool",
        "tool.call",
        parent_span_id=iteration_id,
        tool_name="read_file",
    )
    recorder.finish_span(tool_id, status="error", error_type="tool_failed")
    planning_id = recorder.start_span(
        "planning",
        "planning.update",
        parent_span_id=iteration_id,
        tool_name="plan_update",
    )
    recorder.finish_span(planning_id, attributes={"step_index": 1, "status": "completed"})
    recorder.finish_span(iteration_id)
    trace = recorder.finish_trace(status="ok", outcome="answered")

    spans = sink.spans
    assert [span.sequence_no for span in spans] == [1, 2, 3, 4, 5]
    assert spans[0].span_id == recorder.root_span_id
    assert spans[0].parent_span_id is None
    assert all(span.trace_id == _context().trace_id for span in spans)
    assert next(span for span in spans if span.span_id == llm_id).parent_span_id == iteration_id
    assert trace.llm_call_count == 1
    assert trace.tool_call_count == 1
    assert trace.planning_call_count == 1
    assert trace.model == "served-model"
    assert trace.input_tokens == 10
    assert trace.output_tokens == 5
    assert trace.status == "ok"
    assert trace.outcome == "answered"


def test_missing_usage_or_mixed_model_keeps_trace_aggregate_unknown() -> None:
    sink = InMemoryTraceSink()
    recorder = TraceRecorder(_context(), sink)

    first = recorder.start_span("llm", "llm.request")
    recorder.finish_span(first, model="model-a", input_tokens=2, output_tokens=3)
    closing = recorder.start_span("llm", "llm.request", attributes={"closing": True})
    recorder.finish_span(closing, model="model-b")
    trace = recorder.finish_trace(status="ok", outcome="forced_answer")

    assert trace.llm_call_count == 2
    assert trace.model is None
    assert trace.input_tokens is None
    assert trace.output_tokens is None


def test_finish_trace_cancels_active_children_and_is_idempotent() -> None:
    sink = InMemoryTraceSink()
    recorder = TraceRecorder(_context(), sink)
    active_id = recorder.start_span("llm", "llm.request")

    first = recorder.finish_trace(status="cancelled", outcome="cancelled")
    second = recorder.finish_trace(status="cancelled", outcome="cancelled")

    active = next(span for span in sink.spans if span.span_id == active_id)
    assert active.status == "cancelled"
    assert active.error_type == "unfinished_span"
    assert first == second
    assert len(sink.traces) == 1


def test_invalid_trace_terminal_pair_does_not_mutate_active_trace() -> None:
    sink = InMemoryTraceSink()
    recorder = TraceRecorder(_context(), sink)
    active_id = recorder.start_span("llm", "llm.request")

    with pytest.raises(ValueError, match="successful outcome"):
        recorder.finish_trace(status="ok", outcome="failed")

    assert recorder.finish_span(active_id) is not None
    assert recorder.finish_trace(status="ok", outcome="answered").status == "ok"


def test_failed_trace_marks_active_children_error_with_safe_error_type() -> None:
    sink = InMemoryTraceSink()
    recorder = TraceRecorder(_context(), sink)
    active_id = recorder.start_span("llm", "llm.request")

    trace = recorder.finish_trace(
        status="error",
        outcome="failed",
        error_type="APIConnectionError",
    )

    active = next(span for span in sink.spans if span.span_id == active_id)
    assert active.status == "error"
    assert active.error_type == "APIConnectionError"
    assert trace.status == "error"


class RaisingSink:
    def __init__(self, failing_method: str, marker: str) -> None:
        self.failing_method = failing_method
        self.marker = marker

    def _maybe_raise(self, method: str) -> None:
        if method == self.failing_method:
            raise RuntimeError(self.marker)

    def start_trace(self, trace) -> None:  # noqa: ANN001
        self._maybe_raise("start_trace")

    def start_span(self, span) -> None:  # noqa: ANN001
        self._maybe_raise("start_span")

    def finish_span(self, span) -> None:  # noqa: ANN001
        self._maybe_raise("finish_span")

    def finish_trace(self, trace) -> None:  # noqa: ANN001
        self._maybe_raise("finish_trace")


@pytest.mark.parametrize(
    "failing_method",
    ["start_trace", "start_span", "finish_span", "finish_trace"],
)
def test_sink_failures_never_escape_or_log_exception_payload(
    failing_method: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "private-sink-exception-payload"
    caplog.set_level(logging.WARNING)

    recorder = TraceRecorder(_context(), RaisingSink(failing_method, marker))
    span_id = recorder.start_span("tool", "tool.call", tool_name="safe_tool")
    assert recorder.finish_span(span_id) is not None
    trace = recorder.finish_trace(status="ok", outcome="answered")

    assert trace.status == "ok"
    assert marker not in caplog.text


def test_sink_receives_detached_snapshots() -> None:
    class MutatingSink(InMemoryTraceSink):
        def start_span(self, span: SpanRecord) -> None:
            span.attributes["iteration"] = 999
            super().start_span(span)

    sink = MutatingSink()
    recorder = TraceRecorder(_context(), sink)
    span_id = recorder.start_span(
        "iteration",
        "agent.iteration",
        attributes={"iteration": 1},
    )
    finished = recorder.finish_span(span_id)

    assert finished is not None
    assert finished.attributes == {"iteration": 1}


def test_noop_sink_supports_full_lifecycle() -> None:
    recorder = TraceRecorder(_context(), NoOpTraceSink())
    span_id = recorder.start_span("tool", "tool.call", tool_name="echo")

    assert recorder.finish_span(span_id) is not None
    assert recorder.finish_trace(status="ok", outcome="direct_answer").tool_call_count == 1
