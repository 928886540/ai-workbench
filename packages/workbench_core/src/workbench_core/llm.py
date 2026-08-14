"""Minimal OpenAI-compatible LLM client."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
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
    ) -> str:
        turn = self.chat_turn(
            messages,
            temperature=temperature,
            response_format=response_format,
        )
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
    ) -> ChatTurn:
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

        response = self._client.chat.completions.create(**kwargs)
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

        return ChatTurn(
            content=message.content,
            tool_calls=tool_calls,
            raw_message=raw_message,
        )
