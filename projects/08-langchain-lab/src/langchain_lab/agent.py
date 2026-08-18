"""One high-level LangChain Agent experience, deliberately not a product."""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent

from langchain_lab.tool import lookup_runbook

_SYSTEM_PROMPT = (
    "You are a conservative incident assistant. Use the local runbook tool when useful, "
    "inspect its result, and do not invent operational facts."
)


def build_runbook_agent(model: Any) -> Any:
    """Create one minimal high-level Agent around the lab's Tool."""

    return create_agent(
        model=model,
        tools=[lookup_runbook],
        system_prompt=_SYSTEM_PROMPT,
    )
