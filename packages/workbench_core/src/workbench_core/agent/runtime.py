"""Small reusable tool-calling agent loop."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import CancelledError
from contextlib import contextmanager
from contextvars import ContextVar
from inspect import Parameter, signature
from threading import Event
from typing import Any, Protocol

from workbench_core.agent.events import AgentEvent, AgentResult, ToolStep
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


class AgentCancelled(CancelledError):
    """Raised when a caller cancels an in-flight agent turn."""


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

    def _chat_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        temperature: float,
        cancel_event: Event | None,
    ) -> ChatTurn:
        self._check_cancelled(cancel_event)
        kwargs: dict[str, Any] = {
            "tools": tools,
            "temperature": temperature,
        }
        kwargs.update(self._cancel_kwargs(self.client.chat_turn, cancel_event))
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
    ) -> AgentResult:
        resolved_cancel_event = (
            cancel_event if cancel_event is not None else current_cancel_event()
        )
        with cancellation_scope(resolved_cancel_event):
            return self._run(
                user_message,
                history=history,
                cancel_event=resolved_cancel_event,
            )

    def _run(
        self,
        user_message: str,
        *,
        history: Sequence[dict[str, Any]],
        cancel_event: Event | None,
    ) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            *[dict(message) for message in history],
            {"role": "user", "content": user_message},
        ]
        steps: list[ToolStep] = []
        current_turn = 1

        try:
            self._check_cancelled(cancel_event)
            for turn in range(1, self.max_turns + 1):
                current_turn = turn
                self._check_cancelled(cancel_event)
                self._emit(AgentEvent(kind="turn_started", turn=turn))
                response = self._chat_turn(
                    messages,
                    tools=self.tools.schemas,
                    temperature=self.temperature,
                    cancel_event=cancel_event,
                )

                if response.has_tool_calls:
                    messages.append(response.raw_message)
                    direct_answers: list[str] = []
                    direct_answer_resolver = getattr(self.tools, "direct_answer", None)
                    for call in response.tool_calls:
                        self._check_cancelled(cancel_event)
                        arguments = parse_tool_arguments(call.arguments)
                        self._emit(
                            AgentEvent(
                                kind="tool_started",
                                turn=turn,
                                tool_name=call.name,
                                arguments=arguments,
                            )
                        )
                        self._check_cancelled(cancel_event)
                        if "_error" in arguments:
                            result = {
                                "ok": False,
                                "error": arguments["_error"],
                                "raw": arguments.get("_raw"),
                            }
                        else:
                            result = self.tools.execute(call.name, arguments)
                        self._check_cancelled(cancel_event)
                        direct_answer = (
                            direct_answer_resolver(call.name, arguments, result)
                            if direct_answer_resolver is not None
                            else None
                        )
                        if direct_answer:
                            direct_answers.append(direct_answer)
                        self._check_cancelled(cancel_event)
                        steps.append(ToolStep(call.name, arguments, result))
                        self._emit(
                            AgentEvent(
                                kind="tool_finished",
                                turn=turn,
                                tool_name=call.name,
                                arguments=arguments,
                                result=result,
                            )
                        )
                        self._check_cancelled(cancel_event)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": _compact_result(result, self.result_limit),
                            }
                        )
                    if direct_answers:
                        self._check_cancelled(cancel_event)
                        answer = "\n\n".join(direct_answers)
                        messages.append({"role": "assistant", "content": answer})
                        self._check_cancelled(cancel_event)
                        self._emit(AgentEvent(kind="completed", turn=turn, content=answer))
                        return AgentResult(
                            answer=answer,
                            steps=steps,
                            turns=turn,
                            messages=messages,
                        )
                    self._check_cancelled(cancel_event)
                    continue

                answer = (response.content or "").strip()
                if not answer:
                    answer = "模型没有返回最终答案，也没有继续调用工具。"
                messages.append({"role": "assistant", "content": answer})
                self._check_cancelled(cancel_event)
                self._emit(AgentEvent(kind="completed", turn=turn, content=answer))
                return AgentResult(answer=answer, steps=steps, turns=turn, messages=messages)

            self._check_cancelled(cancel_event)
            messages.append({"role": "user", "content": self.closing_prompt})
            answer = self._chat(
                messages,
                temperature=self.temperature,
                cancel_event=cancel_event,
            )
            self._check_cancelled(cancel_event)
            messages.append({"role": "assistant", "content": answer})
            self._check_cancelled(cancel_event)
            self._emit(AgentEvent(kind="completed", turn=self.max_turns, content=answer))
            return AgentResult(
                answer=answer,
                steps=steps,
                turns=self.max_turns,
                messages=messages,
            )
        except AgentCancelled:
            self._emit(AgentEvent(kind="cancelled", turn=current_turn))
            raise
        except CancelledError as exc:
            if cancel_event is not None and cancel_event.is_set():
                self._emit(AgentEvent(kind="cancelled", turn=current_turn))
                raise AgentCancelled("agent turn cancelled") from exc
            raise
