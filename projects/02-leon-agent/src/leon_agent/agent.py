"""Leon Agent composition root."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from workbench_core.agent import AgentEvent, AgentResult, AgentRuntime
from workbench_core.llm import LLMClient

from leon_agent.leon_client import LeonImageClient
from leon_agent.tools import create_leon_tools

SYSTEM_PROMPT = """
You are Leon Agent, a practical Chinese-speaking personal AI assistant.

You have two responsibilities:
1. Answer ordinary questions directly using the language model.
2. Use Leon image tools when the user asks to generate images or inspect image tasks.

Rules:
- Do not call an image tool for ordinary conversation, architecture discussion, or prompt writing.
- Call generate_images only when the user clearly asks to create or generate an image.
- If no image mode is specified, use the tool's configured default. If the user asks what modes
  exist or requests a particular style without a known id, call list_image_modes first.
- A successful generate_images result means the task was submitted, not that rendering finished.
- Report generation_plan_id and job_id values concisely after submission.
- Use get_image_tasks or get_recent_images when the user asks about status or results.
- Never invent tool results. Explain tool errors directly and suggest the smallest next action.
- Reply in Simplified Chinese unless the user explicitly asks for another language.
""".strip()


class LeonAgent:
    def __init__(
        self,
        *,
        llm_client: LLMClient,
        image_client: LeonImageClient,
        session_id: str,
        default_mode_ids: list[str],
        max_turns: int = 8,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        tools = create_leon_tools(
            image_client,
            session_id=session_id,
            default_mode_ids=default_mode_ids,
        )
        self.runtime = AgentRuntime(
            client=llm_client,
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            max_turns=max_turns,
            temperature=0.2,
            on_event=on_event,
        )

    def run(
        self,
        message: str,
        *,
        history: Sequence[dict[str, Any]] = (),
    ) -> AgentResult:
        return self.runtime.run(message, history=history)
