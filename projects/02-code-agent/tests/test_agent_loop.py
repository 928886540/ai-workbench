from code_agent.agent import CodeAgent, _parse_arguments
from code_agent.workspace import Workspace
from workbench_core.llm import ChatTurn, ToolCall


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def chat_turn(self, messages, tools=None, temperature=0.1):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            return ChatTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="list_dir",
                        arguments=json_args({"relative_path": "."}),
                    )
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "list_dir",
                                "arguments": json_args({"relative_path": "."}),
                            },
                        }
                    ],
                },
            )
        return ChatTurn(
            content="## 项目概览\nOK",
            tool_calls=[],
            raw_message={"role": "assistant", "content": "## 项目概览\nOK"},
        )


def json_args(data):  # noqa: ANN001
    import json

    return json.dumps(data)


def test_parse_arguments_invalid() -> None:
    parsed = _parse_arguments("{bad")
    assert "_error" in parsed


def test_code_agent_loop_with_fake_client(tmp_path):  # noqa: ANN001
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    agent = CodeAgent(
        workspace=Workspace(tmp_path),
        client=FakeClient(),  # type: ignore[arg-type]
        max_turns=3,
        verbose=False,
    )
    result = agent.run("分析一下")
    assert result.turns == 2
    assert len(result.steps) == 1
    assert result.steps[0].name == "list_dir"
    assert "项目概览" in result.answer
