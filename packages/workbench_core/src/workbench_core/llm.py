"""Minimal OpenAI-compatible LLM client.

Why this exists:
- One shared client for all projects
- Keep provider details out of business code
- Start simple; add streaming/tools later when 02-code-agent needs them
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from openai import OpenAI

from workbench_core.config import Settings, get_settings


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = OpenAI(
            api_key=self.settings.require_api_key(),
            base_url=self.settings.llm_base_url,
            timeout=self.settings.llm_timeout_seconds,
            max_retries=self.settings.llm_max_retries,
        )

    @property
    def model(self) -> str:
        return self.settings.llm_model

    def chat(
        self,
        messages: Sequence[ChatMessage | dict[str, str]],
        *,
        temperature: float = 0.2,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        payload = [
            {"role": m.role, "content": m.content} if isinstance(m, ChatMessage) else m
            for m in messages
        ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": payload,
            "temperature": temperature,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        response = self._client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        if content is None:
            raise RuntimeError("LLM returned empty content")
        return content
