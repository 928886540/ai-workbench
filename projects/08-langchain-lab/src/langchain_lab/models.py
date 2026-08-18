"""One small structured-output contract for the component lab."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class IncidentAnalysis(BaseModel):
    severity: Literal["low", "medium", "high", "critical"]
    service: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    needs_runbook: bool
    next_step: str = Field(min_length=1)
