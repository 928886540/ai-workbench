"""Structured output practice for Phase 01.

This module intentionally stays small:
- define a Pydantic schema
- ask the model for JSON
- validate into a typed object
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from workbench_core.llm import ChatMessage, LLMClient


class ProjectBrief(BaseModel):
    name: str
    purpose: str
    stage: Literal["bootstrap", "mvp", "production-hardening"]
    next_actions: list[str] = Field(default_factory=list)


def extract_project_brief(raw_text: str, client: LLMClient | None = None) -> ProjectBrief:
    llm = client or LLMClient()
    prompt = f"""
Extract a project brief as pure JSON with keys:
name, purpose, stage, next_actions.

stage must be one of: bootstrap, mvp, production-hardening

Text:
{raw_text}
""".strip()

    content = llm.chat(
        [
            ChatMessage(role="system", content="Return valid JSON only."),
            ChatMessage(role="user", content=prompt),
        ],
        response_format={"type": "json_object"},
    )
    return ProjectBrief.model_validate(json.loads(content))
