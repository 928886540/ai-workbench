"""Channel-independent Leon image tool service."""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Event
from typing import Any
from uuid import uuid4

from workbench_core.agent.runtime import AgentCancelled, current_cancel_event

from leon_agent.image_modes import mode_catalog_items
from leon_agent.leon_client import LeonImageClient

TERMINAL_IMAGE_STATUSES = {"completed", "failed", "cancelled", "canceled"}


def _job_id(item: dict[str, Any]) -> str:
    return str(item.get("job_id") or "")


def _check_cancelled(cancel_event: Event | None) -> None:
    event = cancel_event if cancel_event is not None else current_cancel_event()
    if event is not None and event.is_set():
        raise AgentCancelled("agent turn cancelled")


def _wait_for_image_results(
    client: LeonImageClient,
    *,
    chat_id: str,
    submission: dict[str, Any],
    timeout_seconds: float = 240.0,
    poll_interval_seconds: float = 2.0,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    """Wait for submitted jobs only when the caller needs a synchronous result."""
    _check_cancelled(cancel_event)
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
        _check_cancelled(cancel_event)
        task_result = client.get_image_tasks(
            chat_id=chat_id,
            limit=max(20, min(100, len(job_ids) * 4)),
        )
        _check_cancelled(cancel_event)
        task_items = task_result.get("items", []) if isinstance(task_result, dict) else []
        latest_tasks = [
            item
            for item in task_items
            if isinstance(item, dict) and _job_id(item) in job_ids
        ]
        task_by_id = {_job_id(item): item for item in latest_tasks}

        all_seen = job_ids.issubset(task_by_id)
        all_terminal = all_seen and all(
            str(task_by_id[job_id].get("status") or "").lower()
            in TERMINAL_IMAGE_STATUSES
            for job_id in job_ids
        )
        if all_terminal:
            _check_cancelled(cancel_event)
            gallery_result = client.get_recent_images(
                chat_id=chat_id,
                limit=max(20, min(100, len(job_ids) * 4)),
            )
            _check_cancelled(cancel_event)
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
            _check_cancelled(cancel_event)
            return {
                **submission,
                "waited_for_completion": True,
                "timed_out": True,
                "tasks": latest_tasks,
                "images": [],
            }
        wait_seconds = max(0.1, poll_interval_seconds)
        event = cancel_event if cancel_event is not None else current_cancel_event()
        if event is not None:
            if event.wait(wait_seconds):
                raise AgentCancelled("agent turn cancelled")
        else:
            time.sleep(wait_seconds)


class LeonToolService:
    """Expose Leon image operations without coupling them to CLI, Web, or MCP."""

    def __init__(
        self,
        client: LeonImageClient,
        *,
        session_id: str,
        default_mode_ids: list[str],
        wait_for_image_completion: bool = True,
        on_generation_submitted: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.client = client
        self.chat_id = f"leon-agent:{session_id}"
        self.default_mode_ids = list(default_mode_ids)
        self.wait_for_image_completion = wait_for_image_completion
        self.on_generation_submitted = on_generation_submitted

    def list_image_modes(self) -> dict[str, Any]:
        result = self.client.list_modes()
        if not isinstance(result, dict):
            return {"ok": False, "modes": [], "error": "Invalid mode catalog response"}
        modes = [item for item in result.get("modes", []) if isinstance(item, dict)]
        return {**result, "modes": mode_catalog_items(modes)}

    def check_image_environment(self) -> dict[str, Any]:
        return self.client.check_environment()

    def generate_images(
        self,
        source_text: str,
        workflow_ids: list[str] | None = None,
        batch_count: int = 1,
        character_context: str = "",
        random_workflow: bool = False,
    ) -> dict[str, Any]:
        _check_cancelled(None)
        modes = workflow_ids or self.default_mode_ids
        submission = self.client.generate_images(
            source_text=source_text,
            workflow_ids=modes,
            batch_count=batch_count,
            chat_id=self.chat_id,
            message_id=f"leon-{int(time.time() * 1000)}-{uuid4().hex[:6]}",
            character_context=character_context,
            random_workflow=random_workflow,
        )
        submission.setdefault("source_text", source_text)
        submission.setdefault("workflow_ids", list(modes))
        if self.on_generation_submitted is not None:
            self.on_generation_submitted(submission)
        _check_cancelled(None)
        if not self.wait_for_image_completion:
            return {
                **submission,
                "waited_for_completion": False,
                "images": submission.get("images", []),
            }
        return _wait_for_image_results(
            self.client,
            chat_id=self.chat_id,
            submission=submission,
        )

    def get_image_tasks(self, limit: int = 20) -> dict[str, Any]:
        return self.client.get_image_tasks(chat_id=self.chat_id, limit=limit)

    def get_recent_images(self, limit: int = 20) -> dict[str, Any]:
        return self.client.get_recent_images(chat_id=self.chat_id, limit=limit)

    def get_latest_images(self, limit: int) -> dict[str, Any]:
        return self.client.get_latest_images(limit=limit)

    def cancel_image_task(self, job_id: str) -> dict[str, Any]:
        return self.client.cancel_image_task(job_id=job_id)
