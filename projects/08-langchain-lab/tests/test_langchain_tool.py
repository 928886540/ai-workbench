from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool
from langchain_lab.tool import lookup_runbook
from pydantic import ValidationError


def test_lookup_runbook_is_a_structured_tool_with_a_schema() -> None:
    assert isinstance(lookup_runbook, StructuredTool)
    schema = lookup_runbook.args_schema.model_json_schema()

    assert schema["required"] == ["service"]
    assert schema["properties"]["service"]["minLength"] == 1
    assert schema["properties"]["service"]["maxLength"] == 64


def test_lookup_runbook_invokes_the_local_handler() -> None:
    result = lookup_runbook.invoke({"service": " Payment "})

    assert result["found"] is True
    assert result["service"] == "payment"
    assert len(result["steps"]) == 2


def test_lookup_runbook_rejects_missing_required_arguments() -> None:
    with pytest.raises(ValidationError):
        lookup_runbook.invoke({})
