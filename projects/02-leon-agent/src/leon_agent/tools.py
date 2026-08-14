"""Model-visible tools backed by the Leon image client."""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from workbench_core.agent import AgentTool, ToolRegistry

from leon_agent.leon_client import LeonImageClient

TERMINAL_IMAGE_STATUSES = {"completed", "failed", "cancelled", "canceled"}


def _job_id(item: dict[str, Any]) -> str:
    return str(item.get("job_id") or "")


def _wait_for_image_results(
    client: LeonImageClient,
    *,
    chat_id: str,
    submission: dict[str, Any],
    timeout_seconds: float = 240.0,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    """Poll existing Leon task/gallery sync APIs until submitted jobs finish.

    This keeps Prompt/Workflow/LoRA ownership in the existing Leon image system. The
    Agent only waits on durable job ids so a single generate_images tool call can return
    the finished image URLs instead of requiring a second user turn.
    """
    job_ids = {
        str(item.get("job_id"))
        for item in submission.get("jobs", [])
        if isinstance(item, dict) and item.get("job_id")
    }
    if not job_ids:
        return {**submission, "waited_for_completion": False, "images": []}

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    latest_tasks: list[dict[str, Any]] = []

    while True:
        task_result = client.get_image_tasks(
            chat_id=chat_id,
            limit=max(20, min(100, len(job_ids) * 4)),
        )
        task_items = task_result.get("items", []) if isinstance(task_result, dict) else []
        latest_tasks = [
            item
            for item in task_items
            if isinstance(item, dict) and _job_id(item) in job_ids
        ]
        task_by_id = {_job_id(item): item for item in latest_tasks}

        all_seen = job_ids.issubset(task_by_id)
        all_terminal = all_seen and all(
            str(task_by_id[job_id].get("status") or "").lower() in TERMINAL_IMAGE_STATUSES
            for job_id in job_ids
        )

        if all_terminal:
            gallery_result = client.get_recent_images(
                chat_id=chat_id,
                limit=max(20, min(100, len(job_ids) * 4)),
            )
            gallery_items = (
                gallery_result.get("items", []) if isinstance(gallery_result, dict) else []
            )
            images = [
                item
                for item in gallery_items
                if isinstance(item, dict) and _job_id(item) in job_ids
            ]
            return {
                **submission,
                "waited_for_completion": True,
                "timed_out": False,
                "tasks": latest_tasks,
                "images": images,
            }

        if time.monotonic() >= deadline:
            return {
                **submission,
                "waited_for_completion": True,
                "timed_out": True,
                "tasks": latest_tasks,
                "images": [],
            }

        time.sleep(max(0.1, poll_interval_seconds))


def create_leon_tools(
    client: LeonImageClient,
    *,
    session_id: str,
    default_mode_ids: list[str],
) -> ToolRegistry:
    chat_id = f"leon-agent:{session_id}"

    def generate_images(
        source_text: str,
        workflow_ids: list[str] | None = None,
        batch_count: int = 1,
        character_context: str = "",
        random_workflow: bool = False,
    ) -> dict:
        modes = workflow_ids or default_mode_ids
        submission = client.generate_images(
            source_text=source_text,
            workflow_ids=modes,
            batch_count=batch_count,
            chat_id=chat_id,
            message_id=f"cli-{int(time.time() * 1000)}-{uuid4().hex[:6]}",
            character_context=character_context,
            random_workflow=random_workflow,
        )
        return _wait_for_image_results(
            client,
            chat_id=chat_id,
            submission=submission,
        )

    return ToolRegistry(
        [
            AgentTool(
                name="list_image_modes",
                description="List image modes available in the installed Leon image plugin.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=client.list_modes,
            ),
            AgentTool(
                name="check_image_environment",
                description=(
                    "Check Leon backend connectivity and required ComfyUI nodes and LoRAs. "
                    "This does not submit an image task."
                ),
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=client.check_environment,
            ),
            AgentTool(
                name="generate_images",
                description=(
                    "Generate images through the existing Leon/ComfyUI pipeline. Call only when "
                    "the user explicitly asks to create or generate images. The tool waits for "
                    "the submitted jobs to finish and returns completed image records when they "
                    "are available. source_text must preserve the user's image request rather than "
                    "rewriting it into a new prompt. batch_count is per selected mode."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_text": {
                            "type": "string",
                            "description": (
                                "The user's original image request to pass through to the existing "
                                "Leon prompt pipeline with minimal rewriting."
                            ),
                        },
                        "workflow_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional mode ids from list_image_modes.",
                        },
                        "batch_count": {
                            "type": "integer",
                            "enum": [1, 2, 3, 5, 10],
                            "default": 1,
                        },
                        "character_context": {
                            "type": "string",
                            "description": (
                                "Optional stable character identity and appearance context."
                            ),
                        },
                        "random_workflow": {"type": "boolean", "default": False},
                    },
                    "required": ["source_text"],
                    "additionalProperties": False,
                },
                handler=generate_images,
            ),
            AgentTool(
                name="get_image_tasks",
                description="Get recent image task states created by this Leon Agent session.",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
                    },
                    "additionalProperties": False,
                },
                handler=lambda limit=20: client.get_image_tasks(chat_id=chat_id, limit=limit),
            ),
            AgentTool(
                name="get_recent_images",
                description="Get recent completed images created by this Leon Agent session.",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
                    },
                    "additionalProperties": False,
                },
                handler=lambda limit=20: client.get_recent_images(chat_id=chat_id, limit=limit),
            ),
        ]
    )
