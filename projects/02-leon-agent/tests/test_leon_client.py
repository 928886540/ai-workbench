from pathlib import Path

import httpx
from leon_agent.leon_client import LeonImageClient


class FakeBridge:
    def run(self, action: str, **payload):  # noqa: ANN003
        if action == "list_modes":
            return {"modes": [{"id": "k2_tifa", "family": "krea2"}]}
        if action == "build_request":
            return {
                "outputCount": 1,
                "body": {
                    "generation_plan_id": "leon-plan-1",
                    "source_text": payload["options"]["sourceText"],
                },
            }
        if action == "inspect_environment":
            return {"ok": True, "modeCount": 1}
        raise AssertionError(action)


def test_generate_images_returns_durable_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ios/async_autogen"
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "job_id": "job-1",
                        "client_task_id": "client-1",
                        "status": "queued",
                        "workflow_name": "k2_tifa",
                    }
                ]
            },
        )

    client = LeonImageClient(
        backend_url="http://leon.test",
        plugin_dir=Path("unused"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        bridge=FakeBridge(),
    )

    result = client.generate_images(
        source_text="雨夜街头",
        workflow_ids=["k2_tifa"],
        batch_count=1,
        chat_id="leon-agent:test",
        message_id="message-1",
    )

    assert result["generation_plan_id"] == "leon-plan-1"
    assert result["jobs"][0]["job_id"] == "job-1"


def test_task_sync_returns_compact_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ios/image_tasks/sync"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "jobId": "job-1",
                        "status": "running",
                        "workflowName": "k2_tifa",
                        "sourceText": "雨夜街头",
                        "unusedLargeField": "ignored",
                    }
                ]
            },
        )

    client = LeonImageClient(
        backend_url="http://leon.test",
        plugin_dir=Path("unused"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        bridge=FakeBridge(),
    )

    result = client.get_image_tasks(chat_id="leon-agent:test")

    assert result["items"] == [
        {
            "job_id": "job-1",
            "status": "running",
            "progress": None,
            "workflow_name": "k2_tifa",
            "source_text": "雨夜街头",
            "image_url": None,
            "error": None,
        }
    ]
