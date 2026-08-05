"""Minimal smoke entry for LLM connectivity.

Run:
  uv run python -m llm_core.hello
"""

from __future__ import annotations

from workbench_core.config import get_settings
from workbench_core.llm import ChatMessage, LLMClient
from workbench_core.logging import setup_logging


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    client = LLMClient(settings)
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
