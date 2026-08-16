from typing import Any

from leon_agent.service import LeonToolService


class FakeImageClient:
    def __init__(self) -> None:
        self.generate_calls: list[dict[str, Any]] = []
        self.task_calls: list[dict[str, Any]] = []

    def list_modes(self) -> dict[str, Any]:
        return {"ok": True, "modes": [{"id": "k2_queen_marika"}]}

    def check_environment(self) -> dict[str, Any]:
        return {"ok": True, "modeCount": 19}

    def generate_images(self, **kwargs: Any) -> dict[str, Any]:
        self.generate_calls.append(kwargs)
        return {"ok": True, "generation_plan_id": "plan-1", "jobs": []}

    def get_image_tasks(self, **kwargs: Any) -> dict[str, Any]:
        self.task_calls.append(kwargs)
        return {"ok": True, "items": []}

    def get_recent_images(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "items": [], "request": kwargs}

    def get_latest_images(self, *, limit: int) -> dict[str, Any]:
        return {"ok": True, "items": [], "limit": limit}

    def cancel_image_task(self, *, job_id: str) -> dict[str, Any]:
        return {"ok": True, "job_id": job_id, "cancelled": True}


def create_service(client: FakeImageClient) -> LeonToolService:
    return LeonToolService(
        client,  # type: ignore[arg-type]
        session_id="mcp-demo",
        default_mode_ids=["k2_queen_marika"],
        wait_for_image_completion=False,
    )


def test_service_normalizes_modes_and_checks_environment() -> None:
    service = create_service(FakeImageClient())

    modes = service.list_image_modes()

    assert modes["modes"][0]["name"] == "玛莉卡"
    assert service.check_image_environment() == {"ok": True, "modeCount": 19}


def test_service_generation_uses_channel_independent_session_scope() -> None:
    client = FakeImageClient()
    service = create_service(client)

    result = service.generate_images("生成一张雨夜人像")

    assert result["waited_for_completion"] is False
    assert result["workflow_ids"] == ["k2_queen_marika"]
    assert client.generate_calls[0]["chat_id"] == "leon-agent:mcp-demo"
    assert client.generate_calls[0]["source_text"] == "生成一张雨夜人像"
    assert client.generate_calls[0]["message_id"].startswith("leon-")


def test_service_query_and_cancel_methods_preserve_session_scope() -> None:
    client = FakeImageClient()
    service = create_service(client)

    assert service.get_image_tasks(7)["ok"] is True
    assert client.task_calls == [{"chat_id": "leon-agent:mcp-demo", "limit": 7}]
    assert service.get_recent_images(3)["request"] == {
        "chat_id": "leon-agent:mcp-demo",
        "limit": 3,
    }
    assert service.get_latest_images(2)["limit"] == 2
    assert service.cancel_image_task("job-1")["job_id"] == "job-1"
