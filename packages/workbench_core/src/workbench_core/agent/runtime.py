"""Small reusable tool-calling agent loop."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import CancelledError
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from inspect import Parameter, signature
from threading import Event
from typing import Any, Literal, Protocol

from workbench_core.agent.events import AgentEvent, AgentResult, ToolStep
from workbench_core.agent.tracing import TraceContext, TraceRecorder, TraceSink
from workbench_core.llm import ChatTurn, LLMClient


class ToolExecutor(Protocol):
    @property
    def schemas(self) -> list[dict[str, Any]]: ...

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...

    def direct_answer(
        self,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> str | None: ...


EventHandler = Callable[[AgentEvent], None]
TraceOutcome = Literal["answered", "direct_answer", "forced_answer"]


@dataclass(frozen=True, slots=True)
class _RunResult:
    result: AgentResult
    outcome: TraceOutcome


class AgentCancelled(CancelledError):
    """Raised when a caller cancels an in-flight agent turn.

    ``partial_result`` contains only completed, audit-projected tool steps. It
    deliberately excludes assistant content and the raw in-memory transcript so
    callers may persist the side-effect audit without persisting a cancelled turn.
    """

    def __init__(
        self,
        message: str = "agent turn cancelled",
        *,
        partial_result: AgentResult | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_result = partial_result


_cancel_event: ContextVar[Event | None] = ContextVar(
    "workbench_agent_cancel_event",
    default=None,
)


def current_cancel_event() -> Event | None:
    """Return the cancellation event active in the current agent context."""
    return _cancel_event.get()


@contextmanager
def cancellation_scope(cancel_event: Event | None) -> Iterator[None]:
    """Make one per-turn cancellation event visible to nested tools."""
    token = _cancel_event.set(cancel_event)
    try:
        yield
    finally:
        _cancel_event.reset(token)


def parse_tool_arguments(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip() or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw, "_error": "arguments is not valid JSON"}
    if not isinstance(data, dict):
        return {"_raw": raw, "_error": "arguments must be a JSON object"}
    return data


def _compact_result(result: dict[str, Any], limit: int) -> str:
    text = json.dumps(result, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...<truncated {len(text) - limit} chars>"


def _audit_payload(
    tools: ToolExecutor,
    method_name: str,
    tool_name: str,
    payload: dict[str, Any],
    *,
    hidden_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Create a detached event/audit view and fail closed on projection errors."""

    try:
        detached = deepcopy(payload)
        for key in hidden_keys:
            detached.pop(key, None)
        projector = getattr(tools, method_name, None)
        if callable(projector):
            projected = projector(tool_name, detached)
        else:
            projected = detached
        if not isinstance(projected, dict):
            raise TypeError("audit projection must return a dict")
        audited = deepcopy(projected)
        for key in hidden_keys:
            audited.pop(key, None)
        return audited
    except Exception:  # noqa: BLE001 - audit output must fail closed
        return {"audit_error": "projection_failed"}


def _audit_tool_name(tools: ToolExecutor, tool_name: str) -> str:
    """Use an executor's safe name view while keeping legacy executors compatible."""

    projector = getattr(tools, "audit_name", None)
    if not callable(projector):
        return tool_name
    try:
        audited_name = projector(tool_name)
    except Exception:  # noqa: BLE001 - audit output must fail closed
        return "unknown_tool"
    if not isinstance(audited_name, str) or not audited_name:
        return "unknown_tool"
    return audited_name


def _trace_tool_kind(
    tools: ToolExecutor,
    tool_name: str,
) -> Literal["tool", "planning"]:
    """Resolve a registered trace kind while preserving legacy executors."""

    resolver = getattr(tools, "span_kind", None)
    if not callable(resolver):
        return "tool"
    try:
        kind = resolver(tool_name)
    except Exception:  # noqa: BLE001 - trace metadata must fail closed
        return "tool"
    return kind if kind in {"tool", "planning"} else "tool"


def _planning_span_name(tool_name: str) -> str:
    return {
        "plan_create": "planning.create",
        "plan_update": "planning.update",
        "plan_get": "planning.get",
    }.get(tool_name, "planning.call")


def _trace_error_type(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if name.isascii() and name.replace("_", "").isalnum() else "runtime_error"


def _trace_usage_value(usage: dict[str, int] | None, key: str) -> int | None:
    value = usage.get(key) if usage is not None else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class AgentRuntime:
    """Run one conversation turn until the model returns a final answer."""

    def __init__(
        self,
        *,
        client: LLMClient,
        tools: ToolExecutor,
        system_prompt: str,
        max_turns: int = 8,
        temperature: float = 0.2,
        result_limit: int = 6000,
        closing_prompt: str = "Stop calling tools and answer using the evidence collected so far.",
        on_event: EventHandler | None = None,
    ) -> None:
        self.client = client
        self.tools = tools
        self.system_prompt = system_prompt.strip()
        self.max_turns = max_turns
        self.temperature = temperature
        self.result_limit = result_limit
        self.closing_prompt = closing_prompt
        self.on_event = on_event

    def _emit(self, event: AgentEvent) -> None:
        if self.on_event is not None:
            self.on_event(event)

    @staticmethod
    def _check_cancelled(cancel_event: Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise AgentCancelled("agent turn cancelled")

    @staticmethod
    def _cancel_kwargs(method: Callable[..., Any], cancel_event: Event | None) -> dict[str, Any]:
        if cancel_event is None:
            return {}
        try:
            parameters = signature(method).parameters.values()
        except (TypeError, ValueError):
            return {"cancel_event": cancel_event}
        supports_cancel = any(
            parameter.name == "cancel_event" or parameter.kind == Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        return {"cancel_event": cancel_event} if supports_cancel else {}

    @staticmethod
    def _delta_kwargs(
        method: Callable[..., Any], on_delta: Callable[[str], None] | None
    ) -> dict[str, Any]:
        if on_delta is None:
            return {}
        try:
            parameters = signature(method).parameters.values()
        except (TypeError, ValueError):
            return {"on_delta": on_delta}
        supports_delta = any(
            parameter.name == "on_delta" or parameter.kind == Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        return {"on_delta": on_delta} if supports_delta else {}

    def _chat_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        temperature: float,
        cancel_event: Event | None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatTurn:
        self._check_cancelled(cancel_event)
        kwargs: dict[str, Any] = {
            "tools": tools,
            "temperature": temperature,
        }
        kwargs.update(self._cancel_kwargs(self.client.chat_turn, cancel_event))
        kwargs.update(self._delta_kwargs(self.client.chat_turn, on_delta))
        try:
            response = self.client.chat_turn(messages, **kwargs)
        except CancelledError as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise AgentCancelled("agent turn cancelled") from exc
            raise
        self._check_cancelled(cancel_event)
        return response

    def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        cancel_event: Event | None,
    ) -> str:
        self._check_cancelled(cancel_event)
        kwargs: dict[str, Any] = {"temperature": temperature}
        kwargs.update(self._cancel_kwargs(self.client.chat, cancel_event))
        try:
            answer = self.client.chat(messages, **kwargs)
        except CancelledError as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise AgentCancelled("agent turn cancelled") from exc
            raise
        self._check_cancelled(cancel_event)
        return answer

    def run(
        self,
        user_message: str,
        *,
        history: Sequence[dict[str, Any]] = (),
        cancel_event: Event | None = None,
        system_context: str | None = None,
        trace_context: TraceContext | None = None,
        trace_sink: TraceSink | None = None,
    ) -> AgentResult:
        resolved_cancel_event = (
            cancel_event if cancel_event is not None else current_cancel_event()
        )
        trace = (
            TraceRecorder(trace_context, trace_sink)
            if trace_context is not None
            else None
        )
        try:
            with cancellation_scope(resolved_cancel_event):
                run_result = self._run(
                    user_message,
                    history=history,
                    cancel_event=resolved_cancel_event,
                    system_context=system_context,
                    trace=trace,
                    trace_context=trace_context,
                )
        except AgentCancelled:
            if trace is not None:
                trace.finish_trace(status="cancelled", outcome="cancelled")
            raise
        except CancelledError as exc:
            if trace is not None:
                trace.finish_trace(
                    status="cancelled",
                    outcome="cancelled",
                    error_type=_trace_error_type(exc),
                )
            raise
        except BaseException as exc:
            if trace is not None:
                trace.finish_trace(
                    status="error",
                    outcome="failed",
                    error_type=_trace_error_type(exc),
                )
            raise
        if trace is not None:
            trace.finish_trace(status="ok", outcome=run_result.outcome)
        return run_result.result

    def _run(
        self,
        user_message: str,
        *,
        history: Sequence[dict[str, Any]],
        cancel_event: Event | None,
        system_context: str | None,
        trace: TraceRecorder | None,
        trace_context: TraceContext | None,
    ) -> _RunResult:
        context_message = (
            {"role": "system", "content": system_context.strip()}
            if isinstance(system_context, str) and system_context.strip()
            else None
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            *([context_message] if context_message is not None else []),
            *[dict(message) for message in history],
            {"role": "user", "content": user_message},
        ]
        steps: list[ToolStep] = []
        current_turn = 1
        usage_totals: dict[str, int] = {}
        served_model: str | None = None
        requested_model: str | None = None
        if trace is not None:
            try:
                requested_model = (
                    str(getattr(self.client, "model", "") or "").strip() or None
                )
            except Exception:  # noqa: BLE001 - trace metadata must not affect the request
                requested_model = None

        def emit(
            event: AgentEvent,
            *,
            span_id: str | None = None,
            parent_span_id: str | None = None,
        ) -> None:
            if trace_context is not None:
                event = replace(
                    event,
                    trace_id=trace_context.trace_id,
                    turn_id=trace_context.turn_id,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                )
            self._emit(event)

        def result_with_trace(**kwargs: Any) -> AgentResult:
            if trace_context is not None:
                kwargs["trace_id"] = trace_context.trace_id
                kwargs["turn_id"] = trace_context.turn_id
            return AgentResult(**kwargs)

        def accumulate_usage(response: ChatTurn) -> None:
            nonlocal served_model
            if response.usage:
                for key, value in response.usage.items():
                    usage_totals[key] = usage_totals.get(key, 0) + value
            if response.model:
                served_model = response.model

        def usage_snapshot() -> dict[str, int] | None:
            return dict(usage_totals) if usage_totals else None

        def partial_audit_result() -> AgentResult | None:
            if not steps:
                return None
            return result_with_trace(
                answer="",
                steps=deepcopy(steps),
                turns=current_turn,
                messages=[],
            )

        current_iteration_span_id: str | None = None
        try:
            self._check_cancelled(cancel_event)
            for turn in range(1, self.max_turns + 1):
                current_turn = turn
                self._check_cancelled(cancel_event)
                current_iteration_span_id = (
                    trace.start_span(
                        "iteration",
                        "agent.iteration",
                        attributes={"iteration": turn},
                    )
                    if trace is not None
                    else None
                )
                emit(
                    AgentEvent(kind="turn_started", turn=turn),
                    span_id=current_iteration_span_id,
                    parent_span_id=trace.root_span_id if trace is not None else None,
                )
                tool_schemas = self.tools.schemas

                llm_span_id = (
                    trace.start_span(
                        "llm",
                        "llm.request",
                        parent_span_id=current_iteration_span_id,
                        attributes={
                            "iteration": turn,
                            "streaming": True,
                            "requested_model": requested_model,
                            "message_count": len(messages),
                            "tool_schema_count": len(tool_schemas),
                        },
                    )
                    if trace is not None
                    else None
                )

                def emit_delta(
                    piece: str,
                    turn: int = turn,
                    llm_span_id: str | None = llm_span_id,
                    iteration_span_id: str | None = current_iteration_span_id,
                ) -> None:
                    emit(
                        AgentEvent(kind="assistant_delta", turn=turn, content=piece),
                        span_id=llm_span_id,
                        parent_span_id=iteration_span_id,
                    )

                response = self._chat_turn(
                    messages,
                    tools=tool_schemas,
                    temperature=self.temperature,
                    cancel_event=cancel_event,
                    on_delta=emit_delta,
                )
                if trace is not None and llm_span_id is not None:
                    response_model = response.model or requested_model
                    trace.finish_span(
                        llm_span_id,
                        model=response_model,
                        input_tokens=_trace_usage_value(response.usage, "input_tokens"),
                        output_tokens=_trace_usage_value(response.usage, "output_tokens"),
                        attributes={
                            "served_model": response.model,
                            "model_source": (
                                "served" if response.model is not None else "requested"
                            ),
                        },
                    )
                accumulate_usage(response)

                if response.has_tool_calls:
                    messages.append(response.raw_message)
                    direct_answers: list[str] = []
                    direct_answer_resolver = getattr(self.tools, "direct_answer", None)
                    for call in response.tool_calls:
                        self._check_cancelled(cancel_event)
                        arguments = parse_tool_arguments(call.arguments)
                        parse_failed = "_error" in arguments
                        audited_tool_name = _audit_tool_name(self.tools, call.name)
                        audited_arguments = _audit_payload(
                            self.tools,
                            "audit_arguments",
                            call.name,
                            arguments,
                            hidden_keys=("_raw",) if parse_failed else (),
                        )
                        tool_kind = _trace_tool_kind(self.tools, call.name)
                        tool_span_id = (
                            trace.start_span(
                                tool_kind,
                                (
                                    _planning_span_name(audited_tool_name)
                                    if tool_kind == "planning"
                                    else "tool.call"
                                ),
                                parent_span_id=current_iteration_span_id,
                                attributes={"iteration": turn},
                                tool_name=audited_tool_name,
                            )
                            if trace is not None
                            else None
                        )
                        emit(
                            AgentEvent(
                                kind="tool_started",
                                turn=turn,
                                tool_name=audited_tool_name,
                                arguments=audited_arguments,
                            ),
                            span_id=tool_span_id,
                            parent_span_id=current_iteration_span_id,
                        )
                        self._check_cancelled(cancel_event)
                        if parse_failed:
                            raw_result = {
                                "ok": False,
                                "error": arguments["_error"],
                                "raw": arguments.get("_raw"),
                            }
                        else:
                            raw_result = self.tools.execute(call.name, arguments)
                        audited_result = _audit_payload(
                            self.tools,
                            "audit_result",
                            call.name,
                            raw_result,
                            hidden_keys=("raw",) if parse_failed else (),
                        )
                        steps.append(
                            ToolStep(
                                audited_tool_name,
                                audited_arguments,
                                audited_result,
                                trace_id=(
                                    trace_context.trace_id
                                    if trace_context is not None
                                    else None
                                ),
                                span_id=tool_span_id,
                            )
                        )
                        tool_failed = parse_failed or raw_result.get("ok") is not True
                        try:
                            emit(
                                AgentEvent(
                                    kind="tool_finished",
                                    turn=turn,
                                    tool_name=audited_tool_name,
                                    arguments=audited_arguments,
                                    result=audited_result,
                                ),
                                span_id=tool_span_id,
                                parent_span_id=current_iteration_span_id,
                            )
                        finally:
                            if trace is not None and tool_span_id is not None:
                                trace.finish_span(
                                    tool_span_id,
                                    status="error" if tool_failed else "ok",
                                    error_type=(
                                        "invalid_arguments"
                                        if parse_failed
                                        else "tool_error" if tool_failed else None
                                    ),
                                )
                        # Once a handler returns, its side effect may already be durable.
                        # Publish the audit record before observing a concurrent cancel.
                        self._check_cancelled(cancel_event)
                        direct_answer = (
                            direct_answer_resolver(call.name, arguments, raw_result)
                            if direct_answer_resolver is not None and not parse_failed
                            else None
                        )
                        if direct_answer:
                            direct_answers.append(direct_answer)
                        self._check_cancelled(cancel_event)
                        # ``messages`` is the in-memory LLM transcript. Persisted/event audit
                        # consumers receive only ``audited_arguments``/``audited_result`` above.
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": _compact_result(raw_result, self.result_limit),
                            }
                        )
                    if direct_answers:
                        self._check_cancelled(cancel_event)
                        answer = "\n\n".join(direct_answers)
                        messages.append({"role": "assistant", "content": answer})
                        self._check_cancelled(cancel_event)
                        emit(
                            AgentEvent(kind="completed", turn=turn, content=answer),
                            span_id=current_iteration_span_id,
                            parent_span_id=(
                                trace.root_span_id if trace is not None else None
                            ),
                        )
                        self._check_cancelled(cancel_event)
                        if trace is not None and current_iteration_span_id is not None:
                            trace.finish_span(current_iteration_span_id)
                        return _RunResult(
                            result_with_trace(
                                answer=answer,
                                steps=steps,
                                turns=turn,
                                messages=messages,
                                model=served_model,
                                usage=usage_snapshot(),
                            ),
                            "direct_answer",
                        )
                    self._check_cancelled(cancel_event)
                    if trace is not None and current_iteration_span_id is not None:
                        trace.finish_span(current_iteration_span_id)
                    current_iteration_span_id = None
                    continue

                answer = (response.content or "").strip()
                if not answer:
                    answer = "模型没有返回最终答案，也没有继续调用工具。"
                messages.append({"role": "assistant", "content": answer})
                self._check_cancelled(cancel_event)
                emit(
                    AgentEvent(kind="completed", turn=turn, content=answer),
                    span_id=current_iteration_span_id,
                    parent_span_id=trace.root_span_id if trace is not None else None,
                )
                self._check_cancelled(cancel_event)
                if trace is not None and current_iteration_span_id is not None:
                    trace.finish_span(current_iteration_span_id)
                return _RunResult(
                    result_with_trace(
                        answer=answer,
                        steps=steps,
                        turns=turn,
                        messages=messages,
                        model=served_model,
                        usage=usage_snapshot(),
                    ),
                    "answered",
                )

            self._check_cancelled(cancel_event)
            messages.append({"role": "user", "content": self.closing_prompt})
            closing_iteration = self.max_turns + 1
            current_iteration_span_id = (
                trace.start_span(
                    "iteration",
                    "agent.iteration",
                    attributes={"iteration": closing_iteration, "closing": True},
                )
                if trace is not None
                else None
            )
            closing_llm_span_id = (
                trace.start_span(
                    "llm",
                    "llm.request",
                    parent_span_id=current_iteration_span_id,
                    attributes={
                        "iteration": closing_iteration,
                        "closing": True,
                        "streaming": False,
                        "requested_model": requested_model,
                        "message_count": len(messages),
                    },
                )
                if trace is not None
                else None
            )
            answer = self._chat(
                messages,
                temperature=self.temperature,
                cancel_event=cancel_event,
            )
            if trace is not None and closing_llm_span_id is not None:
                trace.finish_span(
                    closing_llm_span_id,
                    attributes={"model_source": "unavailable"},
                )
            self._check_cancelled(cancel_event)
            messages.append({"role": "assistant", "content": answer})
            self._check_cancelled(cancel_event)
            emit(
                AgentEvent(kind="completed", turn=self.max_turns, content=answer),
                span_id=current_iteration_span_id,
                parent_span_id=trace.root_span_id if trace is not None else None,
            )
            self._check_cancelled(cancel_event)
            if trace is not None and current_iteration_span_id is not None:
                trace.finish_span(current_iteration_span_id)
            return _RunResult(
                result_with_trace(
                    answer=answer,
                    steps=steps,
                    turns=self.max_turns,
                    messages=messages,
                    model=served_model,
                    usage=usage_snapshot(),
                ),
                "forced_answer",
            )
        except AgentCancelled as exc:
            partial_result = partial_audit_result()
            if partial_result is not None:
                exc.partial_result = partial_result
            emit(
                AgentEvent(kind="cancelled", turn=current_turn),
                span_id=current_iteration_span_id,
                parent_span_id=trace.root_span_id if trace is not None else None,
            )
            raise
        except CancelledError as exc:
            if cancel_event is not None and cancel_event.is_set():
                emit(
                    AgentEvent(kind="cancelled", turn=current_turn),
                    span_id=current_iteration_span_id,
                    parent_span_id=trace.root_span_id if trace is not None else None,
                )
                raise AgentCancelled(
                    "agent turn cancelled",
                    partial_result=partial_audit_result(),
                ) from exc
            raise
