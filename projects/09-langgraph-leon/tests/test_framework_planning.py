from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda
from leon_framework.planning import PlanOutput, build_model_planner, validate_plan_steps
from pydantic import ValidationError


class FakeStructuredModel:
    def __init__(self) -> None:
        self.schema: type[PlanOutput] | None = None
        self.prompt_text = ""

    def with_structured_output(self, schema: type[PlanOutput]) -> RunnableLambda:
        self.schema = schema

        def respond(prompt: Any) -> PlanOutput:
            self.prompt_text = "\n".join(
                str(message.content) for message in prompt.to_messages()
            )
            return PlanOutput(
                steps=[
                    "Inspect the relevant files.",
                    "Use the available read tools.",
                    "Summarize the evidence.",
                ]
            )

        return RunnableLambda(respond)


def test_model_planner_builds_bounded_steps_without_provider() -> None:
    model = FakeStructuredModel()
    planner = build_model_planner(model, ["file_search", "read_file"])

    steps = planner([HumanMessage(content="分析当前项目结构")])

    assert model.schema is PlanOutput
    assert len(steps) == 3
    assert "file_search, read_file" in model.prompt_text
    assert "分析当前项目结构" in model.prompt_text


@pytest.mark.parametrize(
    "steps",
    [
        ["only one"],
        ["one", "two", "three", "four", "five"],
        ["one", "line\nbreak"],
    ],
)
def test_plan_validation_rejects_unbounded_steps(steps: list[str]) -> None:
    with pytest.raises(ValidationError):
        validate_plan_steps(steps)
