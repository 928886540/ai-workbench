"""SDK-neutral, payload-free tracing primitives for the shared agent runtime."""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Literal, Protocol
from uuid import uuid4

logger = logging.getLogger(__name__)

TraceEntrypoint = Literal["cli", "web", "eval", "direct"]
SpanKind = Literal["agent", "iteration", "llm", "tool", "planning"]
SpanStatus = Literal["running", "ok", "error", "cancelled"]
TraceOutcome = Literal[
    "answered",
    "direct_answer",
    "forced_answer",
    "failed",
    "cancelled",
]
AttributeValue = bool | int | float | str | None

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_ENTRYPOINTS = frozenset({"cli", "web", "eval", "direct"})
_SPAN_KINDS = frozenset({"agent", "iteration", "llm", "tool", "planning"})
_SPAN_STATUSES = frozenset({"running", "ok", "error", "cancelled"})
_TRACE_OUTCOMES = frozenset(
    {"answered", "direct_answer", "forced_answer", "failed", "cancelled"}
)
_SUCCESS_OUTCOMES = frozenset({"answered", "direct_answer", "forced_answer"})
_MAX_METADATA_STRING_CHARS = 160
_MAX_SESSION_ID_CHARS = 160
_REDACTION_FAILED = "redaction_failed"
_ALLOWED_ATTRIBUTE_KEYS = frozenset(
    {
        "closing",
        "iteration",
        "message_count",
        "model_source",
        "requested_model",
        "result_count",
        "served_model",
        "step_count",
        "step_index",
        "status",
        "streaming",
        "tool_schema_count",
        "truncated",
        "redaction_failed",
    }
)


def new_trace_id() -> str:
    return uuid4().hex


def new_turn_id() -> str:
    return uuid4().hex


def new_span_id() -> str:
    return uuid4().hex[:16]


def _require_id(value: str, *, field_name: str, pattern: re.Pattern[str]) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be lowercase hexadecimal with the required length")


def _safe_string(value: object, *, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        return _REDACTION_FAILED
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > _MAX_METADATA_STRING_CHARS
        or any(not character.isprintable() for character in cleaned)
    ):
        return _REDACTION_FAILED
    return cleaned


def _safe_session_id(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_SESSION_ID_CHARS
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError("session_id must be a bounded printable string or None")
    return value


def project_trace_attributes(
    attributes: Mapping[str, object] | None,
) -> dict[str, AttributeValue]:
    """Project metadata through a fixed allowlist without retaining unsafe values."""

    if attributes is None:
        return {}
    if not isinstance(attributes, Mapping):
        return {_REDACTION_FAILED: True}

    projected: dict[str, AttributeValue] = {}
    truncated = False
    for key, value in attributes.items():
        if key not in _ALLOWED_ATTRIBUTE_KEYS:
            continue
        if key in {"redaction_failed", "truncated"}:
            if value is not True:
                return {_REDACTION_FAILED: True}
            projected[key] = True
            continue
        if value is None or isinstance(value, bool) or isinstance(value, int):
            projected[key] = value
            continue
        if isinstance(value, float):
            if not math.isfinite(value):
                return {_REDACTION_FAILED: True}
            projected[key] = value
            continue
        if isinstance(value, str):
            if len(value) > _MAX_METADATA_STRING_CHARS:
                truncated = True
                continue
            if any(not character.isprintable() for character in value):
                return {_REDACTION_FAILED: True}
            projected[key] = value
            continue
        return {_REDACTION_FAILED: True}
    if truncated:
        projected["truncated"] = True
    return projected


def _validate_timestamp(value: int | None, *, field_name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{field_name} must be a non-negative integer or None")


def _validate_duration(value: float | None) -> None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise ValueError("duration_ms must be a finite non-negative number or None")


def _validate_count(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _safe_token_count(value: object) -> tuple[int | None, bool]:
    if value is None:
        return None, False
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, True
    return value, False


def _validate_trace_terminal_pair(status: SpanStatus, outcome: TraceOutcome | None) -> None:
    if status == "ok" and outcome not in _SUCCESS_OUTCOMES:
        raise ValueError("ok traces require a successful outcome")
    if status == "error" and outcome != "failed":
        raise ValueError("error traces require failed outcome")
    if status == "cancelled" and outcome != "cancelled":
        raise ValueError("cancelled traces require cancelled outcome")


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: str
    turn_id: str
    session_id: str | None
    entrypoint: TraceEntrypoint

    def __post_init__(self) -> None:
        _require_id(self.trace_id, field_name="trace_id", pattern=_TRACE_ID_RE)
        _require_id(self.turn_id, field_name="turn_id", pattern=_TRACE_ID_RE)
        object.__setattr__(self, "session_id", _safe_session_id(self.session_id))
        if self.entrypoint not in _ENTRYPOINTS:
            raise ValueError("entrypoint is not supported")

    @classmethod
    def create(
        cls,
        *,
        session_id: str | None = None,
        entrypoint: TraceEntrypoint = "direct",
        trace_id: str | None = None,
        turn_id: str | None = None,
    ) -> TraceContext:
        return cls(
            trace_id=new_trace_id() if trace_id is None else trace_id,
            turn_id=new_turn_id() if turn_id is None else turn_id,
            session_id=session_id,
            entrypoint=entrypoint,
        )


@dataclass(frozen=True, slots=True)
class SpanRecord:
    span_id: str
    trace_id: str
    parent_span_id: str | None
    sequence_no: int
    kind: SpanKind
    name: str
    started_at_ms: int
    ended_at_ms: int | None = None
    duration_ms: float | None = None
    status: SpanStatus = "running"
    error_type: str | None = None
    attributes: dict[str, AttributeValue] = field(default_factory=dict)
    tool_name: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        _require_id(self.span_id, field_name="span_id", pattern=_SPAN_ID_RE)
        _require_id(self.trace_id, field_name="trace_id", pattern=_TRACE_ID_RE)
        if self.parent_span_id is not None:
            _require_id(
                self.parent_span_id,
                field_name="parent_span_id",
                pattern=_SPAN_ID_RE,
            )
        _validate_count(self.sequence_no, field_name="sequence_no")
        if self.sequence_no == 0:
            raise ValueError("sequence_no must be positive")
        if self.kind not in _SPAN_KINDS:
            raise ValueError("span kind is not supported")
        object.__setattr__(self, "name", _safe_string(self.name, allow_none=False))
        _validate_timestamp(self.started_at_ms, field_name="started_at_ms")
        _validate_timestamp(self.ended_at_ms, field_name="ended_at_ms")
        _validate_duration(self.duration_ms)
        if self.status not in _SPAN_STATUSES:
            raise ValueError("span status is not supported")
        if self.status == "running" and (
            self.ended_at_ms is not None or self.duration_ms is not None
        ):
            raise ValueError("running spans cannot have end timing")
        if self.status != "running" and (self.ended_at_ms is None or self.duration_ms is None):
            raise ValueError("finished spans require end timing")
        object.__setattr__(self, "error_type", _safe_string(self.error_type))
        object.__setattr__(self, "tool_name", _safe_string(self.tool_name))
        object.__setattr__(self, "model", _safe_string(self.model))
        object.__setattr__(self, "attributes", project_trace_attributes(self.attributes))
        for field_name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            safe_value, invalid = _safe_token_count(value)
            if invalid:
                raise ValueError(f"{field_name} must be a non-negative integer or None")
            object.__setattr__(self, field_name, safe_value)


@dataclass(frozen=True, slots=True)
class TraceRecord:
    trace_id: str
    turn_id: str
    session_id: str | None
    entrypoint: TraceEntrypoint
    root_span_id: str
    started_at_ms: int
    ended_at_ms: int | None = None
    duration_ms: float | None = None
    status: SpanStatus = "running"
    outcome: TraceOutcome | None = None
    error_type: str | None = None
    model: str | None = None
    llm_call_count: int = 0
    tool_call_count: int = 0
    planning_call_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        _require_id(self.trace_id, field_name="trace_id", pattern=_TRACE_ID_RE)
        _require_id(self.turn_id, field_name="turn_id", pattern=_TRACE_ID_RE)
        _require_id(self.root_span_id, field_name="root_span_id", pattern=_SPAN_ID_RE)
        object.__setattr__(self, "session_id", _safe_session_id(self.session_id))
        if self.entrypoint not in _ENTRYPOINTS:
            raise ValueError("entrypoint is not supported")
        _validate_timestamp(self.started_at_ms, field_name="started_at_ms")
        _validate_timestamp(self.ended_at_ms, field_name="ended_at_ms")
        _validate_duration(self.duration_ms)
        if self.status not in _SPAN_STATUSES:
            raise ValueError("trace status is not supported")
        if self.status == "running":
            if (
                self.ended_at_ms is not None
                or self.duration_ms is not None
                or self.outcome is not None
            ):
                raise ValueError("running traces cannot have terminal fields")
        elif self.ended_at_ms is None or self.duration_ms is None or self.outcome is None:
            raise ValueError("finished traces require end timing and outcome")
        if self.outcome is not None and self.outcome not in _TRACE_OUTCOMES:
            raise ValueError("trace outcome is not supported")
        if self.status != "running":
            _validate_trace_terminal_pair(self.status, self.outcome)
        object.__setattr__(self, "error_type", _safe_string(self.error_type))
        object.__setattr__(self, "model", _safe_string(self.model))
        for field_name in ("llm_call_count", "tool_call_count", "planning_call_count"):
            _validate_count(getattr(self, field_name), field_name=field_name)
        for field_name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            safe_value, invalid = _safe_token_count(value)
            if invalid:
                raise ValueError(f"{field_name} must be a non-negative integer or None")
            object.__setattr__(self, field_name, safe_value)


class TraceSink(Protocol):
    """Write-only trace destination; reading is an adapter-specific concern."""

    def start_trace(self, trace: TraceRecord) -> None: ...

    def start_span(self, span: SpanRecord) -> None: ...

    def finish_span(self, span: SpanRecord) -> None: ...

    def finish_trace(self, trace: TraceRecord) -> None: ...


class NoOpTraceSink:
    def start_trace(self, trace: TraceRecord) -> None:
        del trace

    def start_span(self, span: SpanRecord) -> None:
        del span

    def finish_span(self, span: SpanRecord) -> None:
        del span

    def finish_trace(self, trace: TraceRecord) -> None:
        del trace


class InMemoryTraceSink:
    """Detached in-memory snapshots for tests and local diagnostics."""

    def __init__(self) -> None:
        self._traces: dict[str, TraceRecord] = {}
        self._spans: dict[str, SpanRecord] = {}
        self._lock = RLock()

    @property
    def traces(self) -> list[TraceRecord]:
        with self._lock:
            return deepcopy(list(self._traces.values()))

    @property
    def spans(self) -> list[SpanRecord]:
        with self._lock:
            return deepcopy(sorted(self._spans.values(), key=lambda item: item.sequence_no))

    def start_trace(self, trace: TraceRecord) -> None:
        with self._lock:
            self._traces[trace.trace_id] = deepcopy(trace)

    def start_span(self, span: SpanRecord) -> None:
        with self._lock:
            self._spans[span.span_id] = deepcopy(span)

    def finish_span(self, span: SpanRecord) -> None:
        with self._lock:
            self._spans[span.span_id] = deepcopy(span)

    def finish_trace(self, trace: TraceRecord) -> None:
        with self._lock:
            self._traces[trace.trace_id] = deepcopy(trace)


def _wall_clock_ms() -> int:
    return int(time.time() * 1000)


def _monotonic_ms() -> float:
    return time.monotonic() * 1000


class TraceRecorder:
    """Own one trace lifecycle while isolating the observed code from sink failures."""

    def __init__(
        self,
        context: TraceContext,
        sink: TraceSink | None = None,
        *,
        wall_clock_ms: Callable[[], int] | None = None,
        monotonic_ms: Callable[[], float] | None = None,
    ) -> None:
        self.context = context
        self.sink: TraceSink = sink if sink is not None else NoOpTraceSink()
        self._wall_clock_ms = wall_clock_ms or _wall_clock_ms
        self._monotonic_ms = monotonic_ms or _monotonic_ms
        self._lock = RLock()
        self._sequence_no = 0
        self._active: dict[str, tuple[SpanRecord, float]] = {}
        self._finished: dict[str, SpanRecord] = {}
        self._final_trace: TraceRecord | None = None

        started_at_ms = self._read_wall_clock()
        started_monotonic_ms = self._read_monotonic_clock()
        self.root_span_id = new_span_id()
        root = self._new_span(
            span_id=self.root_span_id,
            kind="agent",
            name="agent.run",
            parent_span_id=None,
            attributes=None,
            tool_name=None,
            started_at_ms=started_at_ms,
        )
        self._active[root.span_id] = (root, started_monotonic_ms)
        self._trace = TraceRecord(
            trace_id=context.trace_id,
            turn_id=context.turn_id,
            session_id=context.session_id,
            entrypoint=context.entrypoint,
            root_span_id=root.span_id,
            started_at_ms=started_at_ms,
        )
        self._notify("start_trace", self._trace)
        self._notify("start_span", root)

    def _read_wall_clock(self) -> int:
        value = self._wall_clock_ms()
        _validate_timestamp(value, field_name="wall_clock_ms")
        assert value is not None
        return value

    def _read_monotonic_clock(self) -> float:
        value = self._monotonic_ms()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError("monotonic_ms must return a finite number")
        return float(value)

    def _next_sequence(self) -> int:
        self._sequence_no += 1
        return self._sequence_no

    def _new_span(
        self,
        *,
        span_id: str,
        kind: SpanKind,
        name: str,
        parent_span_id: str | None,
        attributes: Mapping[str, object] | None,
        tool_name: str | None,
        started_at_ms: int,
    ) -> SpanRecord:
        return SpanRecord(
            span_id=span_id,
            trace_id=self.context.trace_id,
            parent_span_id=parent_span_id,
            sequence_no=self._next_sequence(),
            kind=kind,
            name=_safe_string(name, allow_none=False) or _REDACTION_FAILED,
            started_at_ms=started_at_ms,
            attributes=project_trace_attributes(attributes),
            tool_name=_safe_string(tool_name),
        )

    def _notify(self, method_name: str, record: TraceRecord | SpanRecord) -> None:
        try:
            callback = getattr(self.sink, method_name)
            callback(deepcopy(record))
        except Exception:  # noqa: BLE001 - observability must not alter Agent execution
            logger.warning("Trace sink callback failed during %s", method_name)

    def start_span(
        self,
        kind: SpanKind,
        name: str,
        *,
        parent_span_id: str | None = None,
        attributes: Mapping[str, object] | None = None,
        tool_name: str | None = None,
    ) -> str:
        with self._lock:
            if self._final_trace is not None:
                raise RuntimeError("trace is already finished")
            resolved_parent = parent_span_id or self.root_span_id
            if resolved_parent not in self._active:
                raise ValueError("parent span must be active in this trace")
            span_id = new_span_id()
            span = self._new_span(
                span_id=span_id,
                kind=kind,
                name=name,
                parent_span_id=resolved_parent,
                attributes=attributes,
                tool_name=tool_name,
                started_at_ms=self._read_wall_clock(),
            )
            self._active[span_id] = (span, self._read_monotonic_clock())
        self._notify("start_span", span)
        return span_id

    def _finish_span_locked(
        self,
        span_id: str,
        *,
        status: SpanStatus,
        error_type: str | None,
        model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        attributes: Mapping[str, object] | None,
    ) -> SpanRecord | None:
        active = self._active.pop(span_id, None)
        if active is None:
            return None
        span, started_monotonic_ms = active
        ended_at_ms = self._read_wall_clock()
        duration_ms = max(0.0, self._read_monotonic_clock() - started_monotonic_ms)
        finish_attributes = project_trace_attributes(attributes)
        if finish_attributes.get(_REDACTION_FAILED) is True:
            merged_attributes = finish_attributes
        else:
            merged_attributes = {**span.attributes, **finish_attributes}
        safe_input_tokens, invalid_input = _safe_token_count(input_tokens)
        safe_output_tokens, invalid_output = _safe_token_count(output_tokens)
        if invalid_input or invalid_output:
            merged_attributes = {_REDACTION_FAILED: True}
        finished = replace(
            span,
            ended_at_ms=ended_at_ms,
            duration_ms=duration_ms,
            status=status,
            error_type=_safe_string(error_type),
            attributes=merged_attributes,
            model=_safe_string(model),
            input_tokens=safe_input_tokens,
            output_tokens=safe_output_tokens,
        )
        self._finished[span_id] = finished
        return finished

    def finish_span(
        self,
        span_id: str,
        *,
        status: SpanStatus = "ok",
        error_type: str | None = None,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> SpanRecord | None:
        if status not in _SPAN_STATUSES - {"running"}:
            raise ValueError("finished span status must be ok, error, or cancelled")
        if span_id == self.root_span_id:
            return None
        with self._lock:
            if self._final_trace is not None:
                return None
            finished = self._finish_span_locked(
                span_id,
                status=status,
                error_type=error_type,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                attributes=attributes,
            )
        if finished is not None:
            self._notify("finish_span", finished)
            return deepcopy(finished)
        return None

    def _aggregate(self) -> tuple[int, int, int, str | None, int | None, int | None]:
        spans = [span for span in self._finished.values() if span.kind != "agent"]
        llm_spans = [span for span in spans if span.kind == "llm"]
        llm_count = len(llm_spans)
        tool_count = sum(span.kind == "tool" for span in spans)
        planning_count = sum(span.kind == "planning" for span in spans)

        models = {span.model for span in llm_spans if span.model is not None}
        model = (
            next(iter(models))
            if llm_spans and len(models) == 1 and all(span.model is not None for span in llm_spans)
            else None
        )

        def total_tokens(field_name: str) -> int | None:
            values = [getattr(span, field_name) for span in llm_spans]
            if not values or any(value is None for value in values):
                return None
            return sum(value for value in values if value is not None)

        return (
            llm_count,
            tool_count,
            planning_count,
            model,
            total_tokens("input_tokens"),
            total_tokens("output_tokens"),
        )

    def finish_trace(
        self,
        *,
        status: SpanStatus,
        outcome: TraceOutcome,
        error_type: str | None = None,
    ) -> TraceRecord:
        if status not in _SPAN_STATUSES - {"running"}:
            raise ValueError("finished trace status must be ok, error, or cancelled")
        if outcome not in _TRACE_OUTCOMES:
            raise ValueError("trace outcome is not supported")
        _validate_trace_terminal_pair(status, outcome)
        with self._lock:
            if self._final_trace is not None:
                return deepcopy(self._final_trace)

            unfinished_ids = sorted(
                (
                    span_id
                    for span_id in self._active
                    if span_id != self.root_span_id
                ),
                key=lambda item: self._active[item][0].sequence_no,
                reverse=True,
            )
            auto_finished: list[SpanRecord] = []
            inherited_status: SpanStatus = status if status != "ok" else "cancelled"
            inherited_error_type = (
                _safe_string(error_type)
                if status != "ok" and error_type is not None
                else "unfinished_span"
            )
            for span_id in unfinished_ids:
                finished = self._finish_span_locked(
                    span_id,
                    status=inherited_status,
                    error_type=inherited_error_type,
                    model=None,
                    input_tokens=None,
                    output_tokens=None,
                    attributes=None,
                )
                if finished is not None:
                    auto_finished.append(finished)

            root_active = self._active.pop(self.root_span_id)
            root, root_started_monotonic_ms = root_active
            ended_at_ms = self._read_wall_clock()
            duration_ms = max(
                0.0,
                self._read_monotonic_clock() - root_started_monotonic_ms,
            )
            finished_root = replace(
                root,
                ended_at_ms=ended_at_ms,
                duration_ms=duration_ms,
                status=status,
                error_type=_safe_string(error_type),
            )
            self._finished[finished_root.span_id] = finished_root
            (
                llm_count,
                tool_count,
                planning_count,
                model,
                input_tokens,
                output_tokens,
            ) = self._aggregate()
            final_trace = replace(
                self._trace,
                ended_at_ms=ended_at_ms,
                duration_ms=duration_ms,
                status=status,
                outcome=outcome,
                error_type=_safe_string(error_type),
                model=model,
                llm_call_count=llm_count,
                tool_call_count=tool_count,
                planning_call_count=planning_count,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            self._final_trace = final_trace

        for span in auto_finished:
            self._notify("finish_span", span)
        self._notify("finish_span", finished_root)
        self._notify("finish_trace", final_trace)
        return deepcopy(final_trace)
