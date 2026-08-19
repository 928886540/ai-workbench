"""Validated contracts for Leon Agent behavior evaluation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalFeatures(StrictModel):
    web_search: bool = True
    files: bool = True
    memory: bool = True
    voice: bool = True


class FakeToolCall(StrictModel):
    name: str = Field(min_length=1)
    arguments: dict[str, object] = Field(default_factory=dict)


class FakeTurn(StrictModel):
    content: str | None = None
    tool_calls: list[FakeToolCall] = Field(default_factory=list)
    transcript_contains: list[str] = Field(default_factory=list)
    transcript_not_contains: list[str] = Field(default_factory=list)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_content_or_tool_call(self) -> FakeTurn:
        if self.content is None and not self.tool_calls:
            raise ValueError("a fake turn needs content or at least one tool call")
        return self


class EvalHistoryMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2_000)


class PlanExpectation(StrictModel):
    mode: Literal["required", "forbidden", "optional"] = "optional"
    min_steps: int = Field(default=2, ge=2, le=8)
    max_steps: int = Field(default=8, ge=2, le=8)

    @model_validator(mode="after")
    def validate_range(self) -> PlanExpectation:
        if self.min_steps > self.max_steps:
            raise ValueError("min_steps cannot exceed max_steps")
        return self


class ToolArgumentsExpectation(StrictModel):
    name: str = Field(min_length=1)
    occurrence: int = Field(default=1, ge=1)
    arguments: dict[str, object] = Field(default_factory=dict)


class EvalExpectations(StrictModel):
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    tool_arguments: list[ToolArgumentsExpectation] = Field(default_factory=list)
    max_tool_calls: int | None = Field(default=None, ge=0)
    plan: PlanExpectation = Field(default_factory=PlanExpectation)
    answer_contains: list[str] = Field(default_factory=list)
    answer_contains_any: list[list[str]] = Field(default_factory=list)
    answer_not_contains: list[str] = Field(default_factory=list)
    audit_not_contains: list[str] = Field(default_factory=list)

    @field_validator(
        "required_tools",
        "forbidden_tools",
        "answer_contains",
        "answer_not_contains",
        "audit_not_contains",
    )
    @classmethod
    def unique_non_empty_values(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("expectation values cannot be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("expectation values must be unique")
        return normalized

    @field_validator("answer_contains_any")
    @classmethod
    def non_empty_alternative_groups(cls, groups: list[list[str]]) -> list[list[str]]:
        normalized_groups: list[list[str]] = []
        for group in groups:
            normalized = [value.strip() for value in group]
            if not normalized or any(not value for value in normalized):
                raise ValueError("answer alternative groups cannot be empty")
            if len(set(normalized)) != len(normalized):
                raise ValueError("answer alternatives must be unique within each group")
            normalized_groups.append(normalized)
        return normalized_groups

    @model_validator(mode="after")
    def disallow_conflicting_tools(self) -> EvalExpectations:
        overlap = set(self.required_tools) & set(self.forbidden_tools)
        if overlap:
            raise ValueError(f"tools cannot be both required and forbidden: {sorted(overlap)}")
        argument_targets = [(item.name, item.occurrence) for item in self.tool_arguments]
        if len(argument_targets) != len(set(argument_targets)):
            raise ValueError("tool argument expectations must target unique occurrences")
        return self


class EvalCase(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    category: str = Field(min_length=1, max_length=64)
    user_message: str = Field(min_length=1, max_length=2_000)
    history: list[EvalHistoryMessage] = Field(default_factory=list)
    allow_live: bool = True
    features: EvalFeatures = Field(default_factory=EvalFeatures)
    expectations: EvalExpectations
    fake_turns: list[FakeTurn] = Field(min_length=1)


class MetricResult(StrictModel):
    passed: bool
    details: list[str] = Field(default_factory=list)


class EvalCaseResult(StrictModel):
    case_id: str
    category: str
    provider: Literal["fake", "live"]
    passed: bool
    answer: str = ""
    error: str | None = None
    latency_ms: float = Field(ge=0)
    turns: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    metrics: dict[str, MetricResult]


class EvalRates(StrictModel):
    task_success: float = Field(ge=0, le=1)
    tool_selection: float = Field(ge=0, le=1)
    plan_adherence: float = Field(ge=0, le=1)
    answer_quality: float = Field(ge=0, le=1)
    safety: float = Field(ge=0, le=1)


class EvalSummary(StrictModel):
    provider: Literal["fake", "live"]
    total_cases: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    rates: EvalRates
    total_latency_ms: float = Field(ge=0)
    total_tool_calls: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    regressions: list[str] = Field(default_factory=list)
    cases: list[EvalCaseResult]
