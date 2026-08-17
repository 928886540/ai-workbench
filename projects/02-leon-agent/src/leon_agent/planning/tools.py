"""AgentTool adapters for the per-turn Planning state machine."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from workbench_core.agent import AgentTool

from leon_agent.planning.service import (
    COMPLETED,
    FAILED,
    IN_PROGRESS,
    MAX_PLAN_STEPS,
    MAX_STEP_DESCRIPTION_CHARS,
    MIN_PLAN_STEPS,
    PENDING,
    PlanningService,
)

_SAFE_STATUSES = frozenset({PENDING, IN_PROGRESS, COMPLETED, FAILED})
_STABLE_ERROR_CODES = frozenset(
    {
        "invalid_argument",
        "invalid_transition",
        "plan_exists",
        "plan_not_found",
        "step_active",
        "step_not_ready",
    }
)


def _audit_failure(result: Mapping[str, Any]) -> dict[str, Any] | None:
    if result.get("ok") is True:
        return None
    error_code = result.get("error_code")
    return {
        "ok": False,
        "error_code": (
            error_code
            if isinstance(error_code, str) and error_code in _STABLE_ERROR_CODES
            else "tool_failed"
        ),
    }


def _audit_result(result: dict[str, Any]) -> dict[str, Any]:
    failure = _audit_failure(result)
    if failure is not None:
        return failure
    raw_steps = result.get("steps")
    if not isinstance(raw_steps, list):
        return {"audit_error": "invalid_result"}
    steps: list[dict[str, Any]] = []
    for expected_index, item in enumerate(raw_steps, start=1):
        if not isinstance(item, Mapping):
            return {"audit_error": "invalid_result"}
        step_index = item.get("step_index")
        status = item.get("status")
        if step_index != expected_index or status not in _SAFE_STATUSES:
            return {"audit_error": "invalid_result"}
        steps.append({"step_index": step_index, "status": status})

    projected: dict[str, Any] = {"ok": True, "steps": steps}
    for key in ("step_count", "completed_count", "failed_count"):
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return {"audit_error": "invalid_result"}
        projected[key] = value
    if projected["step_count"] != len(steps):
        return {"audit_error": "invalid_result"}
    if projected["completed_count"] != sum(
        step["status"] == COMPLETED for step in steps
    ):
        return {"audit_error": "invalid_result"}
    if projected["failed_count"] != sum(step["status"] == FAILED for step in steps):
        return {"audit_error": "invalid_result"}
    active_step = result.get("active_step")
    if active_step is not None and (
        isinstance(active_step, bool)
        or not isinstance(active_step, int)
        or not 1 <= active_step <= len(steps)
    ):
        return {"audit_error": "invalid_result"}
    expected_active_step = next(
        (
            step["step_index"]
            for step in steps
            if step["status"] == IN_PROGRESS
        ),
        None,
    )
    if active_step != expected_active_step:
        return {"audit_error": "invalid_result"}
    projected["active_step"] = active_step
    if not isinstance(result.get("done"), bool):
        return {"audit_error": "invalid_result"}
    expected_done = projected["completed_count"] + projected["failed_count"] == len(steps)
    if result["done"] != expected_done:
        return {"audit_error": "invalid_result"}
    projected["done"] = result["done"]
    return projected


def _audit_create_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    steps = arguments.get("steps")
    if not isinstance(steps, list):
        return {"audit_error": "invalid_argument"}
    return {"step_count": len(steps)}


def _audit_update_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    step_index = arguments.get("step_index")
    status = arguments.get("status")
    if (
        isinstance(step_index, bool)
        or not isinstance(step_index, int)
        or not 1 <= step_index <= MAX_PLAN_STEPS
        or status not in {IN_PROGRESS, COMPLETED, FAILED}
    ):
        return {"audit_error": "invalid_argument"}
    return {"step_index": step_index, "status": status}


def create_planning_tools(service: PlanningService) -> list[AgentTool]:
    return [
        AgentTool(
            name="plan_create",
            description=(
                "Create one ordered plan for a genuinely multi-step request before calling "
                "domain tools. Do not use this for ordinary chat or a single tool action."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": MIN_PLAN_STEPS,
                        "maxItems": MAX_PLAN_STEPS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": MAX_STEP_DESCRIPTION_CHARS,
                                }
                            },
                            "required": ["description"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
            handler=service.create,
            audit_arguments=_audit_create_arguments,
            audit_result=_audit_result,
            span_kind="planning",
        ),
        AgentTool(
            name="plan_update",
            description=(
                "Move one plan step through pending -> in_progress -> completed or failed. "
                "Mark it in_progress before doing its work and terminal immediately afterward."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "step_index": {"type": "integer", "minimum": 1, "maximum": MAX_PLAN_STEPS},
                    "status": {
                        "type": "string",
                        "enum": [IN_PROGRESS, COMPLETED, FAILED],
                    },
                },
                "required": ["step_index", "status"],
                "additionalProperties": False,
            },
            handler=service.update,
            audit_arguments=_audit_update_arguments,
            audit_result=_audit_result,
            span_kind="planning",
        ),
        AgentTool(
            name="plan_get",
            description="Read the current turn's plan when a long tool chain needs its position.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=service.get,
            audit_arguments=lambda arguments: {},
            audit_result=_audit_result,
            span_kind="planning",
        ),
    ]
