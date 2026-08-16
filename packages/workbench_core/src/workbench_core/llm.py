"""Minimal OpenAI-compatible LLM client."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from threading import Event
from typing import Any

from openai import OpenAI

from workbench_core.config import Settings, get_settings


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatTurn:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_message: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] | None = None
    model: str | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model_override: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._model_override = (model_override or "").strip() or None
        self._client = OpenAI(
            api_key=self.settings.require_api_key(),
            base_url=self.settings.active_base_url,
            timeout=self.settings.llm_timeout_seconds,
            max_retries=self.settings.llm_max_retries,
        )

    @property
    def model(self) -> str:
        return self._model_override or self.settings.active_model

    @property
    def profile(self) -> str:
        return self.settings.profile

    def list_models(self) -> list[str]:
        """Fetch the model catalog exposed by the active OpenAI-compatible provider."""
        response = self._client.models.list()
        items = getattr(response, "data", response)
        model_ids = {
            str(getattr(item, "id", "")).strip()
            for item in items
            if str(getattr(item, "id", "")).strip()
        }
        return sorted(model_ids, key=str.casefold)

    def chat(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        *,
        temperature: float = 0.2,
        response_format: dict[str, Any] | None = None,
        cancel_event: Event | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "temperature": temperature,
            "response_format": response_format,
        }
        if cancel_event is not None:
            kwargs["cancel_event"] = cancel_event
        turn = self.chat_turn(messages, **kwargs)
        if turn.content is None:
            raise RuntimeError("LLM returned empty content")
        return turn.content

    def chat_turn(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        response_format: dict[str, Any] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        cancel_event: Event | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatTurn:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("LLM request cancelled")
        payload = [
            {"role": m.role, "content": m.content} if isinstance(m, ChatMessage) else m
            for m in messages
        ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": payload,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if response_format is not None:
            kwargs["response_format"] = response_format

        if on_delta is not None:
            return self._chat_turn_streaming(
                kwargs, cancel_event=cancel_event, on_delta=on_delta
            )

        response = self._client.chat.completions.create(**kwargs)
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("LLM request cancelled")
        message = response.choices[0].message

        tool_calls: list[ToolCall] = []
        raw_tool_calls: list[dict[str, Any]] = []
        for item in message.tool_calls or []:
            fn = item.function
            tool_calls.append(
                ToolCall(
                    id=item.id,
                    name=fn.name,
                    arguments=fn.arguments or "{}",
                )
            )
            raw_tool_calls.append(
                {
                    "id": item.id,
                    "type": "function",
                    "function": {
                        "name": fn.name,
                        "arguments": fn.arguments or "{}",
                    },
                }
            )

        raw_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
        }
        if raw_tool_calls:
            raw_message["tool_calls"] = raw_tool_calls

        usage_obj = getattr(response, "usage", None)
        usage: dict[str, int] | None = None
        prompt_tokens = getattr(usage_obj, "prompt_tokens", None)
        completion_tokens = getattr(usage_obj, "completion_tokens", None)
        if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
            usage = {"input_tokens": prompt_tokens, "output_tokens": completion_tokens}
        served_model = str(getattr(response, "model", "") or "").strip() or None

        result = ChatTurn(
            content=message.content,
            tool_calls=tool_calls,
            raw_message=raw_message,
            usage=usage,
            model=served_model,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("LLM request cancelled")
        return result

    def _chat_turn_streaming(
        self,
        kwargs: dict[str, Any],
        *,
        cancel_event: Event | None,
        on_delta: Callable[[str], None],
    ) -> ChatTurn:
        """Stream one completion, accumulating the same ChatTurn shape.

        Tool-call arguments arrive as per-index fragments and must be stitched
        back together before the runtime can execute them. The final chunk
        carries usage when the provider honours ``stream_options``; a missing
        usage stays ``None`` so consumers follow the render-nothing rule.
        """
        stream = self._client.chat.completions.create(
            stream=True,
            stream_options={"include_usage": True},
            **kwargs,
        )
        content_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        usage: dict[str, int] | None = None
        served_model: str | None = None
        for chunk in stream:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("LLM request cancelled")
            if served_model is None:
                served_model = str(getattr(chunk, "model", "") or "").strip() or None
            usage_obj = getattr(chunk, "usage", None)
            prompt_tokens = getattr(usage_obj, "prompt_tokens", None)
            completion_tokens = getattr(usage_obj, "completion_tokens", None)
            if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
                usage = {"input_tokens": prompt_tokens, "output_tokens": completion_tokens}
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            piece = getattr(delta, "content", None)
            if piece:
                content_parts.append(piece)
                on_delta(piece)
            for fragment in getattr(delta, "tool_calls", None) or []:
                index = getattr(fragment, "index", 0) or 0
                slot = tool_calls_by_index.setdefault(
                    index, {"id": "", "name": "", "arguments": ""}
                )
                if getattr(fragment, "id", None):
                    slot["id"] = fragment.id
                function = getattr(fragment, "function", None)
                if function is not None:
                    if getattr(function, "name", None):
                        slot["name"] += function.name
                    if getattr(function, "arguments", None):
                        slot["arguments"] += function.arguments

        content = "".join(content_parts) or None
        tool_calls: list[ToolCall] = []
        raw_tool_calls: list[dict[str, Any]] = []
        for index in sorted(tool_calls_by_index):
            slot = tool_calls_by_index[index]
            if not slot["name"]:
                continue
            tool_calls.append(
                ToolCall(
                    id=slot["id"],
                    name=slot["name"],
                    arguments=slot["arguments"] or "{}",
                )
            )
            raw_tool_calls.append(
                {
                    "id": slot["id"],
                    "type": "function",
                    "function": {
                        "name": slot["name"],
                        "arguments": slot["arguments"] or "{}",
                    },
                }
            )
        raw_message: dict[str, Any] = {"role": "assistant", "content": content}
        if raw_tool_calls:
            raw_message["tool_calls"] = raw_tool_calls
        return ChatTurn(
            content=content,
            tool_calls=tool_calls,
            raw_message=raw_message,
            usage=usage,
            model=served_model,
        )
