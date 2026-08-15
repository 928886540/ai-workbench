"""Small reusable tool-calling agent loop."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from workbench_core.agent.events import AgentEvent, AgentResult, ToolStep
from workbench_core.llm import LLMClient


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

    def run(
        self,
        user_message: str,
        *,
        history: Sequence[dict[str, Any]] = (),
    ) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            *[dict(message) for message in history],
            {"role": "user", "content": user_message},
        ]
        steps: list[ToolStep] = []

        for turn in range(1, self.max_turns + 1):
            self._emit(AgentEvent(kind="turn_started", turn=turn))
            response = self.client.chat_turn(
                messages,
                tools=self.tools.schemas,
                temperature=self.temperature,
            )

            if response.has_tool_calls:
                messages.append(response.raw_message)
                direct_answers: list[str] = []
                direct_answer_resolver = getattr(self.tools, "direct_answer", None)
                for call in response.tool_calls:
                    arguments = parse_tool_arguments(call.arguments)
                    self._emit(
                        AgentEvent(
                            kind="tool_started",
                            turn=turn,
                            tool_name=call.name,
                            arguments=arguments,
                        )
                    )
                    if "_error" in arguments:
                        result = {
                            "ok": False,
                            "error": arguments["_error"],
                            "raw": arguments.get("_raw"),
                        }
                    else:
                        result = self.tools.execute(call.name, arguments)
                    steps.append(ToolStep(call.name, arguments, result))
                    direct_answer = (
                        direct_answer_resolver(call.name, arguments, result)
                        if direct_answer_resolver is not None
                        else None
                    )
                    if direct_answer:
                        direct_answers.append(direct_answer)
                    self._emit(
                        AgentEvent(
                            kind="tool_finished",
                            turn=turn,
                            tool_name=call.name,
                            arguments=arguments,
                            result=result,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": _compact_result(result, self.result_limit),
                        }
                    )
                if direct_answers:
                    answer = "\n\n".join(direct_answers)
                    messages.append({"role": "assistant", "content": answer})
                    self._emit(AgentEvent(kind="completed", turn=turn, content=answer))
                    return AgentResult(
                        answer=answer,
                        steps=steps,
                        turns=turn,
                        messages=messages,
                    )
                continue

            answer = (response.content or "").strip()
            if not answer:
                answer = "模型没有返回最终答案，也没有继续调用工具。"
            messages.append({"role": "assistant", "content": answer})
            self._emit(AgentEvent(kind="completed", turn=turn, content=answer))
            return AgentResult(answer=answer, steps=steps, turns=turn, messages=messages)

        messages.append({"role": "user", "content": self.closing_prompt})
        answer = self.client.chat(messages, temperature=self.temperature)
        messages.append({"role": "assistant", "content": answer})
        self._emit(AgentEvent(kind="completed", turn=self.max_turns, content=answer))
        return AgentResult(
            answer=answer,
            steps=steps,
            turns=self.max_turns,
            messages=messages,
        )
