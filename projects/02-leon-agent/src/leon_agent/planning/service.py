"""Bounded, per-turn task planning without a second execution engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Any

MAX_PLAN_STEPS = 8
MIN_PLAN_STEPS = 2
MAX_STEP_DESCRIPTION_CHARS = 160

PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
FAILED = "failed"
TERMINAL_STATUSES = frozenset({COMPLETED, FAILED})
UPDATE_STATUSES = frozenset({IN_PROGRESS, COMPLETED, FAILED})


def _error(error_code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "error": message}


def _has_unsafe_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in value)


@dataclass
class _PlanStep:
    description: str
    status: str = PENDING


class PlanningService:
    """Own one ordered plan that is reset at the start of every Agent turn."""

    def __init__(self) -> None:
        self._steps: list[_PlanStep] = []
        self._lock = RLock()

    def reset(self) -> None:
        with self._lock:
            self._steps = []

    def create(self, steps: Any) -> dict[str, Any]:
        descriptions = self._validate_steps(steps)
        if isinstance(descriptions, dict):
            return descriptions
        with self._lock:
            if self._steps:
                return _error("plan_exists", "A plan already exists for this turn.")
            self._steps = [_PlanStep(description=item) for item in descriptions]
            return self._snapshot()

    def get(self) -> dict[str, Any]:
        with self._lock:
            if not self._steps:
                return _error("plan_not_found", "No plan exists for this turn.")
            return self._snapshot()

    def update(self, step_index: Any, status: Any) -> dict[str, Any]:
        if isinstance(step_index, bool) or not isinstance(step_index, int):
            return _error("invalid_argument", "step_index must be an integer.")
        if not isinstance(status, str) or status not in UPDATE_STATUSES:
            return _error("invalid_argument", "status is not supported.")

        with self._lock:
            if not self._steps:
                return _error("plan_not_found", "No plan exists for this turn.")
            if not 1 <= step_index <= len(self._steps):
                return _error("invalid_argument", "step_index is outside the plan.")

            target = self._steps[step_index - 1]
            if target.status in TERMINAL_STATUSES:
                return _error("invalid_transition", "A terminal step cannot be reopened.")
            if target.status == PENDING:
                if status != IN_PROGRESS:
                    return _error(
                        "invalid_transition",
                        "A pending step must enter in_progress first.",
                    )
                if any(step.status == IN_PROGRESS for step in self._steps):
                    return _error("step_active", "Another plan step is already in progress.")
                if any(
                    step.status not in TERMINAL_STATUSES
                    for step in self._steps[: step_index - 1]
                ):
                    return _error("step_not_ready", "Previous plan steps are not finished.")
            elif target.status == IN_PROGRESS and status not in TERMINAL_STATUSES:
                return _error(
                    "invalid_transition",
                    "An active step must finish as completed or failed.",
                )

            target.status = status
            return self._snapshot()

    @staticmethod
    def _validate_steps(steps: Any) -> list[str] | dict[str, Any]:
        if not isinstance(steps, list) or not MIN_PLAN_STEPS <= len(steps) <= MAX_PLAN_STEPS:
            return _error(
                "invalid_argument",
                f"steps must contain {MIN_PLAN_STEPS} to {MAX_PLAN_STEPS} items.",
            )
        descriptions: list[str] = []
        for item in steps:
            if not isinstance(item, Mapping) or set(item) != {"description"}:
                return _error(
                    "invalid_argument",
                    "Each step must contain only a description.",
                )
            description = item.get("description")
            if not isinstance(description, str):
                return _error("invalid_argument", "Step descriptions must be strings.")
            description = description.strip()
            if (
                not description
                or len(description) > MAX_STEP_DESCRIPTION_CHARS
                or _has_unsafe_control_characters(description)
            ):
                return _error(
                    "invalid_argument",
                    "Step descriptions must be bounded single-line text.",
                )
            descriptions.append(description)
        return descriptions

    def _snapshot(self) -> dict[str, Any]:
        completed_count = sum(step.status == COMPLETED for step in self._steps)
        failed_count = sum(step.status == FAILED for step in self._steps)
        active_step = next(
            (
                index
                for index, step in enumerate(self._steps, start=1)
                if step.status == IN_PROGRESS
            ),
            None,
        )
        return {
            "ok": True,
            "steps": [
                {
                    "step_index": index,
                    "description": step.description,
                    "status": step.status,
                }
                for index, step in enumerate(self._steps, start=1)
            ],
            "step_count": len(self._steps),
            "completed_count": completed_count,
            "failed_count": failed_count,
            "active_step": active_step,
            "done": completed_count + failed_count == len(self._steps),
        }
