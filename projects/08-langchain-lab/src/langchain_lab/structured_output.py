"""LCEL structured-output example: prompt | model | parser."""

from __future__ import annotations

from typing import Any

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import Runnable

from langchain_lab.models import IncidentAnalysis
from langchain_lab.prompt import build_triage_prompt


def build_triage_chain(model: Runnable[Any, Any]) -> Runnable[Any, IncidentAnalysis]:
    parser = PydanticOutputParser(pydantic_object=IncidentAnalysis)
    prompt = build_triage_prompt(format_instructions=parser.get_format_instructions())
    return prompt | model | parser
