"""Model-visible tools backed by the Leon image client."""

from __future__ import annotations

import time
from collections.abc import Callable
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
    """Wait for submitted jobs only when the caller needs a synchronous result."""
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
    wait_for_image_completion: bool = True,
    on_generation_submitted: Callable[[dict[str, Any]], None] | None = None,
) -> ToolRegistry:
    chat_id = f"leon-agent:{session_id}"

    def generate_images(
        source_text: str,
        workflow_ids: list[str] | None = None,
        batch_count: int = 1,
        character_context: str = "",
        random_workflow: bool = False,
    ) -> dict[str, Any]:
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
        if on_generation_submitted is not None:
            on_generation_submitted(submission)
        if not wait_for_image_completion:
            return submission
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
                    "Route an explicit user image-generation request to the existing Leon/ComfyUI "
                    "pipeline. Put the user's current request in source_text verbatim: do not "
                    "translate, rewrite, expand, beautify, or add prompt details. Only pass count, "
                    "exact mode ids, or random mode when the user explicitly supplied them. "
                    "batch_count is per selected mode."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_text": {
                            "type": "string",
                            "description": (
                                "The user's current image request verbatim, without "
                                "prompt rewriting or added visual details."
                            ),
                        },
                        "workflow_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional exact Leon mode ids explicitly requested by the user."
                            ),
                        },
                        "batch_count": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "default": 1,
                            "description": "Images per selected mode; normally 1, 2, 3, 5, or 10.",
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
            AgentTool(
                name="get_latest_images",
                description=(
                    "Get the newest completed images in the global Leon image database, across "
                    "all chats and Leon Agent sessions. Set limit to the exact count requested "
                    "by the user, for example 1 for the latest image or 5 for the latest five."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "description": "Exact number of recent global images requested.",
                        }
                    },
                    "required": ["limit"],
                    "additionalProperties": False,
                },
                handler=lambda limit: client.get_latest_images(limit=limit),
            ),
            AgentTool(
                name="cancel_image_task",
                description=(
                    "Cancel one queued or running image job by job_id and interrupt its "
                    "ComfyUI prompt. Use this whenever the user asks to stop, cancel, abort, "
                    "or 不要了 an image generation. When the user refers to tasks without "
                    "giving ids (for example 'cancel the last three'), call get_image_tasks "
                    "first and cancel each job_id in a separate call. Cancelling a job that "
                    "already finished is safe: the result reports its terminal status instead "
                    "of failing, so report that status truthfully rather than claiming success."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": (
                                "Exact job_id from generate_images or get_image_tasks."
                            ),
                        }
                    },
                    "required": ["job_id"],
                    "additionalProperties": False,
                },
                handler=lambda job_id: client.cancel_image_task(job_id=job_id),
            ),
        ]
    )
