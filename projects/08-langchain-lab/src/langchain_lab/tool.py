"""One small LangChain Tool experiment with an explicit input contract."""

from __future__ import annotations

from langchain_core.tools import tool
from pydantic import BaseModel, Field


class RunbookLookupInput(BaseModel):
    service: str = Field(min_length=1, max_length=64)


_RUNBOOKS: dict[str, tuple[str, ...]] = {
    "payment": (
        "Compare the latest deployment with the previous healthy revision.",
        "Check payment error rate, latency, and downstream dependency health.",
    ),
    "order": (
        "Check order API errors grouped by code and request ID.",
        "Verify database and payment dependency health before rollback.",
    ),
}


@tool(args_schema=RunbookLookupInput)
def lookup_runbook(service: str) -> dict[str, object]:
    """Look up a preconfigured local incident runbook by service name."""

    normalized = service.strip().casefold()
    steps = _RUNBOOKS.get(normalized)
    if steps is None:
        return {
            "found": False,
            "service": normalized,
            "message": "No local runbook found.",
        }
    return {
        "found": True,
        "service": normalized,
        "steps": list(steps),
    }
