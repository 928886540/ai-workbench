from leon_framework.tool_adapter import adapt_leon_tools
from workbench_core.agent import AgentTool, ToolRegistry


def test_adapter_uses_the_same_registry_handler_and_schema() -> None:
    registry = ToolRegistry(
        [
            AgentTool(
                name="echo",
                description="Echo one value.",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=lambda text: {"ok": True, "text": text},
            )
        ]
    )

    tool = adapt_leon_tools(registry)[0]

    assert tool.name == "echo"
    assert tool.invoke({"text": "same-handler"}) == registry.execute(
        "echo", {"text": "same-handler"}
    )


def test_adapter_rejects_unknown_selected_tool() -> None:
    registry = ToolRegistry()

    try:
        adapt_leon_tools(registry, ["missing"])
    except ValueError as exc:
        assert str(exc) == "Unknown Leon tools: missing"
    else:
        raise AssertionError("unknown tool selection must fail")
