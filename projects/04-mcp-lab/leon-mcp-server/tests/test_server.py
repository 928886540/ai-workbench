from __future__ import annotations

import asyncio
from typing import Any

from leon_mcp_server.server import create_mcp_server


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def list_image_modes(self) -> dict[str, Any]:
        self.calls.append(("list_image_modes", None))
        return {"ok": True, "modes": []}

    def check_image_environment(self) -> dict[str, Any]:
        self.calls.append(("check_image_environment", None))
        return {"ok": True}

    def generate_images(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("generate_images", kwargs))
        return {"ok": True, "jobs": [{"job_id": "job-1"}]}

    def get_image_tasks(self, limit: int = 20) -> dict[str, Any]:
        self.calls.append(("get_image_tasks", limit))
        return {"ok": True, "items": []}

    def get_recent_images(self, limit: int = 20) -> dict[str, Any]:
        self.calls.append(("get_recent_images", limit))
        return {"ok": True, "items": []}


def test_mcp_lists_the_five_interview_tools_with_side_effect_annotation() -> None:
    server = create_mcp_server(FakeService())  # type: ignore[arg-type]

    tools = asyncio.run(server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert list(by_name) == [
        "list_image_modes",
        "check_image_environment",
        "generate_images",
        "get_image_tasks",
        "get_recent_images",
    ]
    assert by_name["generate_images"].annotations.readOnlyHint is False
    assert by_name["generate_images"].annotations.idempotentHint is False
    assert by_name["get_image_tasks"].inputSchema["properties"]["limit"]["maximum"] == 100


def test_mcp_generate_tool_calls_service_with_one_image_and_verbatim_text() -> None:
    service = FakeService()
    server = create_mcp_server(service)  # type: ignore[arg-type]
    tool = server._tool_manager.get_tool("generate_images")

    result = asyncio.run(
        tool.run(
            {
                "source_text": "用户原话：雨夜街头，不要改写",
                "workflow_id": "k2_tifa_plus",
            }
        )
    )

    assert result == {"ok": True, "jobs": [{"job_id": "job-1"}]}
    assert service.calls == [
        (
            "generate_images",
            {
                "source_text": "用户原话：雨夜街头，不要改写",
                "workflow_ids": ["k2_tifa_plus"],
                "batch_count": 1,
            },
        )
    ]
