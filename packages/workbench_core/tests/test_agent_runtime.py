import json

from workbench_core.agent import AgentRuntime, AgentTool, ToolRegistry
from workbench_core.llm import ChatTurn, ToolCall


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def chat_turn(self, messages, tools=None, temperature=0.2):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            arguments = json.dumps({"value": "hello"})
            return ChatTurn(
                content=None,
                tool_calls=[ToolCall(id="call-1", name="echo", arguments=arguments)],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "echo", "arguments": arguments},
                        }
                    ],
                },
            )
        return ChatTurn(
            content="done",
            raw_message={"role": "assistant", "content": "done"},
        )


class DirectResultClient:
    def __init__(self) -> None:
        self.calls = 0

    def chat_turn(self, messages, tools=None, temperature=0.2):  # noqa: ANN001
        self.calls += 1
        arguments = json.dumps({"value": "generated"})
        return ChatTurn(
            content=None,
            tool_calls=[ToolCall(id="call-direct", name="generate", arguments=arguments)],
            raw_message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-direct",
                        "type": "function",
                        "function": {
                            "name": "generate",
                            "arguments": arguments,
                        },
                    }
                ],
            },
        )


def test_agent_runtime_executes_registered_tool() -> None:
    registry = ToolRegistry(
        [
            AgentTool(
                name="echo",
                description="Echo a value.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                handler=lambda value: {"ok": True, "value": value},
            )
        ]
    )
    runtime = AgentRuntime(
        client=FakeClient(),  # type: ignore[arg-type]
        tools=registry,
        system_prompt="Use tools when useful.",
    )

    result = runtime.run("echo hello")

    assert result.answer == "done"
    assert result.turns == 2
    assert result.steps[0].result == {"ok": True, "value": "hello"}
    assert registry.names == ["echo"]


def test_agent_runtime_returns_direct_tool_answer_without_second_llm_turn() -> None:
    client = DirectResultClient()
    registry = ToolRegistry(
        [
            AgentTool(
                name="generate",
                description="Generate an image.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                handler=lambda value: {"ok": True, "images": [value]},
                return_direct=True,
                answer_formatter=lambda arguments, result: (
                    f"完成：{result['images'][0]}（{arguments['value']}）"
                ),
            )
        ]
    )
    runtime = AgentRuntime(
        client=client,  # type: ignore[arg-type]
        tools=registry,
        system_prompt="Use tools when useful.",
    )

    result = runtime.run("generate an image")

    assert result.answer == "完成：generated（generated）"
    assert result.turns == 1
    assert client.calls == 1
