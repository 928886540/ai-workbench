from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_lab.agent import build_runbook_agent
from langchain_lab.tool import lookup_runbook
from pydantic import Field


class ToolCallingFakeModel(FakeMessagesListChatModel):
    bound_names: list[str] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Any,
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> ToolCallingFakeModel:
        del tool_choice, kwargs
        self.bound_names = [tool.name for tool in tools]
        return self


def test_high_level_agent_calls_one_tool_then_finishes(monkeypatch) -> None:  # noqa: ANN001
    calls: list[str] = []
    original_handler = lookup_runbook.func
    assert original_handler is not None

    def counted_lookup(service: str) -> dict[str, object]:
        calls.append(service)
        return original_handler(service)

    monkeypatch.setattr(lookup_runbook, "func", counted_lookup)
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "lookup_runbook",
                        "args": {"service": "payment"},
                        "id": "runbook-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Use the payment runbook."),
        ]
    )

    result = build_runbook_agent(model).invoke(
        {"messages": [{"role": "user", "content": "Payment is timing out."}]}
    )

    tool_messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert model.bound_names == ["lookup_runbook"]
    assert calls == ["payment"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "runbook-1"
    assert json.loads(tool_messages[0].content)["found"] is True
    assert result["messages"][-1].content == "Use the payment runbook."
