"""Leon Agent composition root."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from threading import Event
from typing import Any

from workbench_core.agent import AgentEvent, AgentResult, AgentRuntime
from workbench_core.llm import LLMClient

from leon_agent.image_modes import mode_catalog_items
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
- Extract only parameters the user explicitly supplied, such as count, a Leon mode, or random mode.
  Do not choose Prompt, Workflow, LoRA, composition, style, or aesthetics for them.
- The current user turn is authoritative. If it names a mode, resolve that name from the supplied
  mode catalog and never reuse a different mode from an earlier turn.
- If no mode is supplied, use the configured default. Descriptive styles stay inside source_text.
- If a contextual request such as "again" cannot stand alone, ask one short clarification instead
  of inventing the missing image description.
- A successful generate_images result means the task was submitted, not necessarily that rendering
  finished. If the result includes completed images, present their URLs directly and concisely.
- After submission, report the selected mode and task status in Chinese. Do not expose internal
  generation_plan_id, job_id, workflow id, or other machine identifiers unless the user explicitly
  asks for debugging details.
- Use get_image_tasks or get_recent_images for this session's status or results. Use
  get_latest_images for database-wide recent images across chats, and pass limit matching the
  user's requested count exactly (for example latest one -> 1, latest five -> 5).
- When the user asks to stop, cancel, or abort a generation, call cancel_image_task with the
  exact job_id. You can cancel: never tell the user cancelling is unsupported. If the user
  referred to jobs positionally ("the last three"), call get_image_tasks first to resolve the
  ids, then cancel each one in its own call. Report the returned status verbatim, including
  when a job had already finished and could no longer be stopped.
- Never invent tool results. Explain tool errors directly and suggest the smallest next action.
- When speak_text is available and the user asks you to sing, read aloud, or answer with voice,
  call speak_text with the exact words to speak, then keep your text reply to one short line.
  The audio itself is delivered to the chat, so do not paste the spoken text again in full.
- Reply in Simplified Chinese unless the user explicitly asks for another language.
""".strip()


def build_system_prompt(additional_system_prompt: str | None = None) -> str:
    """Append user-managed instructions as part of the system message."""
    if not additional_system_prompt:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n{additional_system_prompt.strip()}"


def image_mode_context(image_client: LeonImageClient) -> str:
    """Give the model current product metadata instead of parsing user text in code."""
    try:
        result = image_client.list_modes()
    except Exception:  # noqa: BLE001 - image generation can still use its configured default
        return ""
    modes = result.get("modes", []) if isinstance(result, dict) else []
    items = mode_catalog_items([item for item in modes if isinstance(item, dict)])
    if not items:
        return ""
    lines = ["Current installed Leon image modes (name -> exact workflow id; aliases):"]
    for item in items:
        aliases = ", ".join(item.get("aliases", [])) or "none"
        lines.append(f"- {item['name']} -> {item['id']} (aliases: {aliases})")
    return "\n".join(lines)


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
        speak_handler: Callable[[str, str | None], dict[str, Any]] | None = None,
        additional_system_prompt: str | None = None,
    ) -> None:
        tools = create_leon_tools(
            image_client,
            session_id=session_id,
            default_mode_ids=default_mode_ids,
            wait_for_image_completion=wait_for_image_completion,
            on_generation_submitted=on_generation_submitted,
            speak_handler=speak_handler,
        )
        system_prompt = build_system_prompt(additional_system_prompt)
        mode_context = image_mode_context(image_client)
        if mode_context:
            system_prompt = f"{system_prompt}\n\n{mode_context}"
        self.runtime = AgentRuntime(
            client=llm_client,
            tools=tools,
            system_prompt=system_prompt,
            max_turns=max_turns,
            temperature=0.2,
            on_event=on_event,
        )

    def run(
        self,
        message: str,
        *,
        history: Sequence[dict[str, Any]] = (),
        cancel_event: Event | None = None,
    ) -> AgentResult:
        return self.runtime.run(message, history=history, cancel_event=cancel_event)
