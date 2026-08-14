"""Minimal smoke entry for LLM connectivity via CC Switch.

Run:
  uv run python -m llm_core.hello
  uv run python -m workbench_core.ccs_cli list
"""

from __future__ import annotations

from workbench_core.config import get_settings, reset_settings_cache
from workbench_core.llm import ChatMessage, LLMClient
from workbench_core.logging import setup_logging


def main() -> None:
    reset_settings_cache()
    settings = get_settings()
    setup_logging(settings.log_level)

    client = LLMClient(settings)
    print(
        f"source={settings.llm_source} profile={settings.profile} "
        f"base_url={settings.active_base_url} model={settings.active_model}"
    )
    text = client.chat(
        [
            ChatMessage(
                role="system",
                content="You are a concise engineering assistant for ai-workbench.",
            ),
            ChatMessage(
                role="user",
                content="Reply with one short sentence confirming the LLM connection works.",
            ),
        ]
    )
    print(text)


if __name__ == "__main__":
    main()
