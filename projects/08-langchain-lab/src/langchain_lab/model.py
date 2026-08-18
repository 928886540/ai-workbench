"""Explicitly opted-in live LangChain model construction."""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
from workbench_core.config import Settings, get_settings


def build_chat_model(settings: Settings | None = None) -> ChatOpenAI:
    active = settings or get_settings()
    kwargs: dict[str, Any] = {
        "api_key": active.require_api_key(),
        "base_url": active.active_base_url,
        "model": active.active_model,
        "temperature": 0,
        "max_retries": active.llm_max_retries,
    }
    if active.llm_timeout_seconds > 0:
        kwargs["timeout"] = active.llm_timeout_seconds
    return ChatOpenAI(**kwargs)
