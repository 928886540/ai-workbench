import json
import threading
import time

import pytest
from workbench_core.agent import AgentCancelled, AgentRuntime, AgentTool, ToolRegistry
from workbench_core.agent.runtime import cancellation_scope
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


def test_agent_runtime_cancelled_before_first_llm_call() -> None:
    class NeverCalledClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat_turn(
            self,
            messages,
            tools=None,
            temperature=0.2,
        ):  # noqa: ANN001, ARG002
            self.calls += 1
            raise AssertionError("cancelled turn must not call the LLM")

    client = NeverCalledClient()
    events = []
    runtime = AgentRuntime(
        client=client,  # type: ignore[arg-type]
        tools=ToolRegistry(),
        system_prompt="Cancel safely.",
        on_event=events.append,
    )
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(AgentCancelled):
        runtime.run("hello", cancel_event=cancel_event)

    assert client.calls == 0
    assert [event.kind for event in events] == ["cancelled"]


def test_agent_runtime_cancel_between_tool_and_next_round() -> None:
    cancel_event = threading.Event()

    class ToolThenAnswerClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat_turn(
            self,
            messages,
            tools=None,
            temperature=0.2,
        ):  # noqa: ANN001, ARG002
            self.calls += 1
            if self.calls == 1:
                arguments = json.dumps({"value": "late"})
                return ChatTurn(
                    content=None,
                    tool_calls=[ToolCall(id="call-1", name="echo", arguments=arguments)],
                    raw_message={"role": "assistant", "content": None},
                )
            return ChatTurn(content="must not be called")

    def echo(value: str) -> dict[str, object]:
        cancel_event.set()
        return {"ok": True, "value": value}

    registry = ToolRegistry(
        [
            AgentTool(
                name="echo",
                description="Echo.",
                parameters={"type": "object"},
                handler=echo,
            )
        ]
    )
    client = ToolThenAnswerClient()
    runtime = AgentRuntime(
        client=client,  # type: ignore[arg-type]
        tools=registry,
        system_prompt="Use tools.",
    )

    with pytest.raises(AgentCancelled):
        runtime.run("echo", cancel_event=cancel_event)

    assert client.calls == 1


def test_agent_runtime_discards_late_blocking_llm_result() -> None:
    started = threading.Event()
    release = threading.Event()
    cancel_event = threading.Event()
    outcome: list[object] = []

    class BlockingClient:
        def chat_turn(self, messages, tools=None, temperature=0.2):  # noqa: ANN001, ARG002
            started.set()
            release.wait(timeout=2)
            return ChatTurn(content="late")

    events = []
    runtime = AgentRuntime(
        client=BlockingClient(),  # type: ignore[arg-type]
        tools=ToolRegistry(),
        system_prompt="Wait safely.",
        on_event=events.append,
    )

    def run() -> None:
        try:
            runtime.run("hello", cancel_event=cancel_event)
        except Exception as exc:  # noqa: BLE001 - capture worker outcome for the assertion
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(timeout=1)
    cancel_event.set()
    time.sleep(0.02)
    assert worker.is_alive()
    release.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert isinstance(outcome[0], AgentCancelled)
    assert all(event.kind != "completed" for event in events)


def test_agent_runtime_discards_late_blocking_tool_result() -> None:
    tool_started = threading.Event()
    release = threading.Event()
    cancel_event = threading.Event()
    outcome: list[object] = []

    class ToolClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat_turn(
            self,
            messages,
            tools=None,
            temperature=0.2,
        ):  # noqa: ANN001, ARG002
            self.calls += 1
            arguments = json.dumps({"value": "late"})
            return ChatTurn(
                content=None,
                tool_calls=[ToolCall(id="call-1", name="wait", arguments=arguments)],
                raw_message={"role": "assistant", "content": None},
            )

    def blocking_tool(value: str) -> dict[str, object]:
        tool_started.set()
        release.wait(timeout=2)
        return {"ok": True, "value": value}

    runtime = AgentRuntime(
        client=ToolClient(),  # type: ignore[arg-type]
        tools=ToolRegistry(
            [
                AgentTool(
                    name="wait",
                    description="Wait.",
                    parameters={"type": "object"},
                    handler=blocking_tool,
                )
            ]
        ),
        system_prompt="Wait safely.",
    )

    def run() -> None:
        try:
            runtime.run("wait", cancel_event=cancel_event)
        except Exception as exc:  # noqa: BLE001 - capture worker outcome for assertion
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert tool_started.wait(timeout=1)
    cancel_event.set()
    release.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert isinstance(outcome[0], AgentCancelled)


def test_agent_runtime_does_not_return_after_completed_callback_requests_cancel() -> None:
    cancel_event = threading.Event()

    class AnswerClient:
        def chat_turn(self, messages, tools=None, temperature=0.2):  # noqa: ANN001, ARG002
            return ChatTurn(content="late")

    def on_event(event) -> None:  # noqa: ANN001
        if event.kind == "completed":
            cancel_event.set()

    runtime = AgentRuntime(
        client=AnswerClient(),  # type: ignore[arg-type]
        tools=ToolRegistry(),
        system_prompt="Check completion boundary.",
        on_event=on_event,
    )

    with pytest.raises(AgentCancelled):
        runtime.run("hello", cancel_event=cancel_event)


def test_cancellation_scope_reaches_runtime_without_changing_default_callers() -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    runtime = AgentRuntime(
        client=NeverClientForScope(),  # type: ignore[arg-type]
        tools=ToolRegistry(),
        system_prompt="Scoped cancellation.",
    )

    with cancellation_scope(cancel_event), pytest.raises(AgentCancelled):
        runtime.run("hello")


class NeverClientForScope:
    def chat_turn(self, messages, tools=None, temperature=0.2):  # noqa: ANN001, ARG002
        raise AssertionError("scoped cancellation must stop before the LLM")
