"""Prompt component kept separate so the learning boundary stays visible."""

from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate


def build_triage_prompt(*, format_instructions: str) -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are a conservative production incident triage assistant. "
                "Do not invent metrics or actions.\n\n{format_instructions}",
            ),
            ("human", "Incident report:\n{incident_text}"),
        ]
    ).partial(format_instructions=format_instructions)
