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
