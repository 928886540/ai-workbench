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
- When the user clearly asks to create, draw, regenerate, or edit an image, route the request to
  generate_images instead of answering with a rewritten image prompt.
- Pass the user's current image request through source_text verbatim whenever it is self-contained.
  Do not translate, summarize, sanitize, expand, beautify, or add visual details to it.
- Extract only parameters the user explicitly supplied, such as count, an exact Leon mode id, or
  random mode. Do not choose Prompt, Workflow, LoRA, composition, style, or aesthetics for them.
- If no exact mode id is supplied, use the configured default. Call list_image_modes only when the
  user asks which modes exist; descriptive styles stay inside source_text.
- If a contextual request such as "again" cannot stand alone, ask one short clarification instead
  of inventing the missing image description.
- A successful generate_images result means the task was submitted, not necessarily that rendering
  finished. If the result includes completed images, present their URLs directly and concisely.
- Report generation_plan_id and job_id values concisely after submission when no image is ready yet.
- Use get_image_tasks or get_recent_images for this session's status or results. Use
  get_latest_image when the user explicitly asks for the database-wide latest image across chats.
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
        wait_for_image_completion: bool = True,
        on_generation_submitted: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        tools = create_leon_tools(
            image_client,
            session_id=session_id,
            default_mode_ids=default_mode_ids,
            wait_for_image_completion=wait_for_image_completion,
            on_generation_submitted=on_generation_submitted,
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
