import json
import threading

import pytest
from workbench_core.agent import (
    AgentCancelled,
    AgentEvent,
    AgentRuntime,
    AgentTool,
    ToolRegistry,
)
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


class ToolCallThenAnswerClient:
    def __init__(self, *, name: str, arguments: str, answer: str = "done") -> None:
        self.name = name
        self.arguments = arguments
        self.answer = answer
        self.calls = 0
        self.second_turn_messages: list[dict[str, object]] = []

    def chat_turn(self, messages, tools=None, temperature=0.2):  # noqa: ANN001, ARG002
        self.calls += 1
        if self.calls == 1:
            return ChatTurn(
                content=None,
                tool_calls=[ToolCall(id="call-scripted", name=self.name, arguments=self.arguments)],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-scripted",
                            "type": "function",
                            "function": {"name": self.name, "arguments": self.arguments},
                        }
                    ],
                },
            )
        self.second_turn_messages = messages
        return ChatTurn(
            content=self.answer,
            raw_message={"role": "assistant", "content": self.answer},
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
    assert result.steps[0].arguments == {"value": "hello"}
    assert result.steps[0].result == {"ok": True, "value": "hello"}
    assert registry.names == ["echo"]


def test_agent_runtime_keeps_legacy_tool_executor_compatible() -> None:
    class LegacyExecutor:
        @property
        def schemas(self) -> list[dict[str, object]]:
            return []

        def execute(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
            assert name == "echo"
            return {"ok": True, **arguments}

    runtime = AgentRuntime(
        client=FakeClient(),  # type: ignore[arg-type]
        tools=LegacyExecutor(),  # type: ignore[arg-type]
        system_prompt="Use tools.",
    )

    result = runtime.run("echo")

    assert result.answer == "done"
    assert result.steps[0].name == "echo"
    assert result.steps[0].arguments == {"value": "hello"}
    assert result.steps[0].result == {"ok": True, "value": "hello"}


def test_tool_registry_audit_projection_is_detached_and_fails_closed() -> None:
    raw_arguments = {"nested": {"private": "unchanged"}}

    def mutate_then_fail(arguments: dict[str, object]) -> dict[str, object]:
        nested = arguments["nested"]
        assert isinstance(nested, dict)
        nested["private"] = "mutated"
        raise RuntimeError("private projection failure")

    registry = ToolRegistry(
        [
            AgentTool(
                name="echo",
                description="Echo.",
                parameters={"type": "object"},
                handler=lambda **arguments: arguments,
                audit_arguments=mutate_then_fail,
                audit_result=lambda result: [],  # type: ignore[arg-type,return-value]
            )
        ]
    )

    assert registry.audit_arguments("echo", raw_arguments) == {
        "audit_error": "projection_failed"
    }
    assert registry.audit_result("echo", {"private": "result"}) == {
        "audit_error": "projection_failed"
    }
    assert raw_arguments == {"nested": {"private": "unchanged"}}


def test_agent_runtime_uses_audit_projection_only_for_events_and_steps() -> None:
    private_argument = "private-argument"
    private_result = "private-result"
    handled: list[dict[str, object]] = []
    events: list[AgentEvent] = []

    def remember(**arguments: object) -> dict[str, object]:
        handled.append(arguments)
        return {
            "ok": True,
            "created": True,
            "updated_at": 123,
            "value": {"note": private_result},
        }

    client = ToolCallThenAnswerClient(
        name="remember",
        arguments=json.dumps(
            {
                "scope": "user",
                "key": "image.preference",
                "value": {"note": private_argument},
            }
        ),
    )
    runtime = AgentRuntime(
        client=client,  # type: ignore[arg-type]
        tools=ToolRegistry(
            [
                AgentTool(
                    name="remember",
                    description="Remember a preference.",
                    parameters={"type": "object"},
                    handler=remember,
                    audit_arguments=lambda arguments: {
                        "scope": arguments["scope"],
                        "key": arguments["key"],
                    },
                    audit_result=lambda result: {
                        "ok": result["ok"],
                        "created": result["created"],
                        "updated_at": result["updated_at"],
                    },
                )
            ]
        ),
        system_prompt="Use tools.",
        on_event=events.append,
    )

    result = runtime.run("remember this")

    assert handled == [
        {
            "scope": "user",
            "key": "image.preference",
            "value": {"note": private_argument},
        }
    ]
    assert result.steps[0].arguments == {
        "scope": "user",
        "key": "image.preference",
    }
    assert result.steps[0].result == {
        "ok": True,
        "created": True,
        "updated_at": 123,
    }
    started = next(event for event in events if event.kind == "tool_started")
    finished = next(event for event in events if event.kind == "tool_finished")
    assert started.arguments == result.steps[0].arguments
    assert finished.arguments == result.steps[0].arguments
    assert finished.result == result.steps[0].result
    assert private_argument not in repr(events)
    assert private_result not in repr(events)
    assert private_argument not in repr(result.steps)
    assert private_result not in repr(result.steps)
    assert private_result in repr(client.second_turn_messages)


def test_agent_runtime_hides_malformed_argument_raw_text_from_audit() -> None:
    malformed = '{"private":"must-not-enter-audit"'
    events: list[AgentEvent] = []
    client = ToolCallThenAnswerClient(
        name="echo",
        arguments=malformed,
        answer="recovered",
    )
    runtime = AgentRuntime(
        client=client,  # type: ignore[arg-type]
        tools=ToolRegistry(
            [
                AgentTool(
                    name="echo",
                    description="Must not format malformed arguments.",
                    parameters={"type": "object"},
                    handler=lambda: {"ok": True},
                    return_direct=True,
                    answer_formatter=lambda arguments, result: str(arguments.get("_raw")),
                )
            ]
        ),
        system_prompt="Handle invalid tool calls.",
        on_event=events.append,
    )

    result = runtime.run("call a malformed tool")

    assert result.answer == "recovered"
    assert client.calls == 2
    started = next(event for event in events if event.kind == "tool_started")
    finished = next(event for event in events if event.kind == "tool_finished")
    assert started.arguments == {"_error": "arguments is not valid JSON"}
    assert finished.arguments == started.arguments
    assert finished.result == {
        "ok": False,
        "error": "arguments is not valid JSON",
    }
    assert result.steps[0].arguments == started.arguments
    assert result.steps[0].result == finished.result
    assert malformed not in repr(events)
    assert malformed not in repr(result.steps)
    tool_message = next(message for message in result.messages if message["role"] == "tool")
    assert json.loads(tool_message["content"])["raw"] == malformed


def test_agent_runtime_fails_closed_for_unknown_tool_audit() -> None:
    private_argument = "must-not-enter-unknown-tool-audit"
    private_tool_name = "unknown-private-tool-name"
    events: list[AgentEvent] = []
    client = ToolCallThenAnswerClient(
        name=private_tool_name,
        arguments=json.dumps({"value": private_argument}),
        answer="recovered",
    )
    runtime = AgentRuntime(
        client=client,  # type: ignore[arg-type]
        tools=ToolRegistry(),
        system_prompt="Handle unknown tools.",
        on_event=events.append,
    )

    result = runtime.run("call an unknown tool")

    started = next(event for event in events if event.kind == "tool_started")
    finished = next(event for event in events if event.kind == "tool_finished")
    assert started.tool_name == "unknown_tool"
    assert finished.tool_name == "unknown_tool"
    assert started.arguments == {"audit_error": "unknown_tool"}
    assert finished.arguments == started.arguments
    assert finished.result == {"audit_error": "unknown_tool"}
    assert result.steps[0].arguments == started.arguments
    assert result.steps[0].result == finished.result
    assert result.steps[0].name == "unknown_tool"
    assert private_argument not in repr(events)
    assert private_argument not in repr(result.steps)
    assert private_tool_name not in repr(events)
    assert private_tool_name not in repr(result.steps)
    tool_call_message = next(message for message in result.messages if "tool_calls" in message)
    raw_arguments = tool_call_message["tool_calls"][0]["function"]["arguments"]
    assert json.loads(raw_arguments)["value"] == private_argument


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


def test_direct_answer_uses_raw_data_while_tool_audit_uses_projection() -> None:
    events: list[AgentEvent] = []
    client = DirectResultClient()
    registry = ToolRegistry(
        [
            AgentTool(
                name="generate",
                description="Generate an image.",
                parameters={"type": "object"},
                handler=lambda value: {"ok": True, "images": [f"private:{value}"]},
                return_direct=True,
                answer_formatter=lambda arguments, result: (
                    f"{arguments['value']} -> {result['images'][0]}"
                ),
                audit_arguments=lambda arguments: {"requested": bool(arguments)},
                audit_result=lambda result: {"ok": result["ok"]},
            )
        ]
    )
    runtime = AgentRuntime(
        client=client,  # type: ignore[arg-type]
        tools=registry,
        system_prompt="Use tools.",
        on_event=events.append,
    )

    result = runtime.run("generate")

    assert result.answer == "generated -> private:generated"
    assert client.calls == 1
    assert result.steps[0].arguments == {"requested": True}
    assert result.steps[0].result == {"ok": True}
    started = next(event for event in events if event.kind == "tool_started")
    finished = next(event for event in events if event.kind == "tool_finished")
    assert started.arguments == {"requested": True}
    assert finished.arguments == {"requested": True}
    assert finished.result == {"ok": True}


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


class StreamingClient:
    def chat_turn(self, messages, tools=None, temperature=0.2, on_delta=None):  # noqa: ANN001
        if on_delta is not None:
            on_delta("he")
            on_delta("llo")
        return ChatTurn(
            content="hello",
            raw_message={"role": "assistant", "content": "hello"},
        )


def test_agent_runtime_emits_assistant_delta_events_in_order() -> None:
    events: list[AgentEvent] = []
    runtime = AgentRuntime(
        client=StreamingClient(),  # type: ignore[arg-type]
        tools=ToolRegistry([]),
        system_prompt="test",
        on_event=events.append,
    )

    result = runtime.run("hi")

    assert result.answer == "hello"
    deltas = [event.content for event in events if event.kind == "assistant_delta"]
    assert deltas == ["he", "llo"]
    assert events[0].kind == "turn_started"
    assert events[-1].kind == "completed"
