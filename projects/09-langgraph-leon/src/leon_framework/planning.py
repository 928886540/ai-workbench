"""Optional model-backed planning for the LangGraph runtime."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field, field_validator

MAX_PLAN_STEPS = 4
MIN_PLAN_STEPS = 2
MAX_STEP_CHARS = 160

Planner = Callable[[Sequence[BaseMessage]], list[str]]


class PlanOutput(BaseModel):
    """Small ordered plan stored directly in LangGraph State."""

    steps: list[str] = Field(min_length=MIN_PLAN_STEPS, max_length=MAX_PLAN_STEPS)

    @field_validator("steps")
    @classmethod
    def normalize_steps(cls, steps: list[str]) -> list[str]:
        normalized: list[str] = []
        for step in steps:
            value = step.strip()
            if (
                not value
                or len(value) > MAX_STEP_CHARS
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError("plan steps must be bounded single-line text")
            normalized.append(value)
        return normalized


_PLANNING_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are the planning node in an agent workflow. Return 2 to 4 short, ordered, "
            "executable steps. Do not answer the request and do not invent capabilities. "
            "Use only the listed tools when a tool is needed.",
        ),
        (
            "human",
            "Available tools: {available_tools}\n\nUser request:\n{request}",
        ),
    ]
)


def validate_plan_steps(steps: Any) -> list[str]:
    """Validate injected/fake planners at the same boundary as the live planner."""

    return PlanOutput.model_validate({"steps": steps}).steps


def format_plan_context(steps: Sequence[str]) -> str:
    """Render checkpointed plan state as ephemeral model context."""

    rendered = "\n".join(f"{index}. {step}" for index, step in enumerate(steps, start=1))
    return (
        "Current execution plan. Follow it in order, using tools only when needed, "
        "then answer from the collected evidence:\n"
        f"{rendered}"
    )


def _latest_user_text(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    raise ValueError("planning requires a user message")


def build_model_planner(model: Any, tool_names: Iterable[str]) -> Planner:
    """Build an opt-in structured planner; invoking it costs one model request."""

    available_tools = ", ".join(tool_names) or "none"
    chain = _PLANNING_PROMPT | model.with_structured_output(PlanOutput)

    def plan(messages: Sequence[BaseMessage]) -> list[str]:
        output = chain.invoke(
            {
                "available_tools": available_tools,
                "request": _latest_user_text(messages),
            }
        )
        parsed = output if isinstance(output, PlanOutput) else PlanOutput.model_validate(output)
        return parsed.steps

    return plan
