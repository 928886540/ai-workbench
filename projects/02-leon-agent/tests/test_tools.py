from typing import Any

from leon_agent.agent import SYSTEM_PROMPT
from leon_agent.tools import create_leon_tools


class FakeImageClient:
    def __init__(self) -> None:
        self.generate_calls: list[dict[str, Any]] = []

    def list_modes(self) -> dict[str, Any]:
        return {"ok": True, "modes": []}

    def check_environment(self) -> dict[str, Any]:
        return {"ok": True}

    def generate_images(self, **kwargs: Any) -> dict[str, Any]:
        self.generate_calls.append(kwargs)
        return {"ok": True, "jobs": []}

    def get_image_tasks(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "items": []}

    def get_recent_images(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "items": []}

    def get_latest_image(self) -> dict[str, Any]:
        return {"ok": True, "item": None}


def test_generate_tool_passes_source_text_verbatim() -> None:
    client = FakeImageClient()
    tools = create_leon_tools(
        client,  # type: ignore[arg-type]
        session_id="session-1",
        default_mode_ids=["k2_tifa_plus"],
    )
    request = "生成一个美女在玩水，来 2 张"

    result = tools.execute(
        "generate_images",
        {"source_text": request, "batch_count": 2},
    )

    assert result["ok"] is True
    assert client.generate_calls[0]["source_text"] == request
    assert client.generate_calls[0]["workflow_ids"] == ["k2_tifa_plus"]
    assert client.generate_calls[0]["batch_count"] == 2


def test_generate_tool_schema_is_gemini_compatible() -> None:
    tools = create_leon_tools(
        FakeImageClient(),  # type: ignore[arg-type]
        session_id="session-1",
        default_mode_ids=["k2_tifa_plus"],
    )
    generate_schema = next(
        item for item in tools.schemas if item["function"]["name"] == "generate_images"
    )
    batch_schema = generate_schema["function"]["parameters"]["properties"][
        "batch_count"
    ]

    assert "enum" not in batch_schema
    assert batch_schema["minimum"] == 1
    assert batch_schema["maximum"] == 10


def test_agent_prompt_forbids_image_prompt_rewriting() -> None:
    assert "source_text verbatim" in SYSTEM_PROMPT
    assert "Do not translate, summarize, sanitize, expand, beautify" in SYSTEM_PROMPT
    assert "Do not choose Prompt, Workflow, LoRA" in SYSTEM_PROMPT
