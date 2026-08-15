from pathlib import Path

import httpx
import pytest
from leon_agent.leon_client import LeonImageClient, LeonImageError


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


def test_cancel_image_task_hits_the_backend_cancel_route() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"ok": True, "job_id": "job-1", "status": "cancelled"})

    client = LeonImageClient(
        backend_url="http://leon.test",
        plugin_dir=Path("unused"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        bridge=FakeBridge(),
    )

    result = client.cancel_image_task(job_id="job-1")

    assert seen == {"method": "POST", "path": "/ios/async_autogen/job-1/cancel"}
    assert result == {"ok": True, "job_id": "job-1", "status": "cancelled", "cancelled": True}


def test_cancel_image_task_reports_an_already_finished_job_truthfully() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "job_id": "job-9", "status": "completed"})

    client = LeonImageClient(
        backend_url="http://leon.test",
        plugin_dir=Path("unused"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        bridge=FakeBridge(),
    )

    result = client.cancel_image_task(job_id="job-9")

    # A finished job cannot be stopped; the agent must not present this as a cancellation.
    assert result["status"] == "completed"
    assert result["cancelled"] is False


def test_cancel_image_task_escapes_the_job_id_path_segment() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # .path is percent-decoded; the wire format is what must stay escaped so a
        # hostile job_id cannot walk out of its path segment.
        seen["raw"] = request.url.raw_path.decode()
        return httpx.Response(200, json={"ok": True, "status": "cancelled"})

    client = LeonImageClient(
        backend_url="http://leon.test",
        plugin_dir=Path("unused"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        bridge=FakeBridge(),
    )

    client.cancel_image_task(job_id="a/b?c")

    assert seen["raw"] == "/ios/async_autogen/a%2Fb%3Fc/cancel"


def test_cancel_image_task_rejects_a_blank_job_id() -> None:
    client = LeonImageClient(
        backend_url="http://leon.test",
        plugin_dir=Path("unused"),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        bridge=FakeBridge(),
    )

    with pytest.raises(LeonImageError):
        client.cancel_image_task(job_id="   ")
