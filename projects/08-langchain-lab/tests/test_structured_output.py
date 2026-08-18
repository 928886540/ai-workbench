import json

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_lab.models import IncidentAnalysis
from langchain_lab.structured_output import build_triage_chain


def test_prompt_model_parser_returns_pydantic_model() -> None:
    response = json.dumps(
        {
            "severity": "medium",
            "service": "order",
            "summary": "订单接口偶发失败。",
            "needs_runbook": False,
            "next_step": "收集请求 ID 和错误码。",
        },
        ensure_ascii=False,
    )
    chain = build_triage_chain(FakeListChatModel(responses=[response]))

    result = chain.invoke({"incident_text": "订单接口偶发失败"})

    assert isinstance(result, IncidentAnalysis)
    assert result.service == "order"
    assert result.needs_runbook is False


def test_parser_rejects_non_json_model_output() -> None:
    chain = build_triage_chain(FakeListChatModel(responses=["not-json"]))

    with pytest.raises(OutputParserException):
        chain.invoke({"incident_text": "无法解析"})
