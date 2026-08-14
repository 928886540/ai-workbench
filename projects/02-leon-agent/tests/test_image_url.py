"""Image URLs returned by Leon tools must be absolute and openable."""

from pathlib import Path

import httpx
import pytest
from leon_agent.config import LeonSettings
from leon_agent.leon_client import LeonImageClient

BACKEND_URL = "http://127.0.0.1:8188"
PUBLIC_URL = "https://comfyui.928886540.xyz"


class FakeBridge:
    def run(self, action: str, **payload):  # noqa: ANN003, ARG002
        raise AssertionError(f"bridge should not be used: {action}")


def build_client(
    *,
    items: list[dict],
    expected_path: str,
    public_base_url: str | None = None,
) -> LeonImageClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == expected_path
        return httpx.Response(200, json={"items": items})

    return LeonImageClient(
        backend_url=BACKEND_URL,
        plugin_dir=Path("unused"),
        public_base_url=public_base_url,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        bridge=FakeBridge(),
    )


def test_task_relative_image_url_is_resolved_against_backend() -> None:
    client = build_client(
        items=[
            {
                "jobId": "job-1",
                "status": "success",
                "workflowName": "k2_tifa",
                "sourceText": "雨夜街头",
                "imageUrl": "/view?filename=leon_00001_.png&type=output",
            }
        ],
        expected_path="/ios/image_tasks/sync",
    )

    item = client.get_image_tasks(chat_id="leon-agent:test")["items"][0]

    assert item["image_url"] == f"{BACKEND_URL}/view?filename=leon_00001_.png&type=output"


def test_task_relative_image_url_prefers_public_base_url() -> None:
    client = build_client(
        items=[
            {
                "jobId": "job-1",
                "status": "success",
                "imageUrl": "view?filename=leon_00002_.png",
            }
        ],
        expected_path="/ios/image_tasks/sync",
        public_base_url=PUBLIC_URL,
    )

    item = client.get_image_tasks(chat_id="leon-agent:test")["items"][0]

    assert item["image_url"] == f"{PUBLIC_URL}/view?filename=leon_00002_.png"


def test_task_snake_case_final_image_url_is_resolved() -> None:
    client = build_client(
        items=[{"job_id": "job-1", "final_image_url": "/view?filename=a.png"}],
        expected_path="/ios/image_tasks/sync",
        public_base_url=PUBLIC_URL,
    )

    item = client.get_image_tasks(chat_id="leon-agent:test")["items"][0]

    assert item["image_url"] == f"{PUBLIC_URL}/view?filename=a.png"


def test_gallery_image_urls_are_absolute() -> None:
    client = build_client(
        items=[
            {"jobId": "job-1", "imageUrl": "/view?filename=a.png"},
            {"jobId": "job-2", "imageUrl": f"{PUBLIC_URL}/view?filename=b.png"},
            {"jobId": "job-3", "imageUrl": "//cdn.example.com/view?filename=c.png"},
            {"jobId": "job-4", "imageUrl": "   "},
            {"jobId": "job-5"},
        ],
        expected_path="/ios/image_gallery/sync",
        public_base_url=PUBLIC_URL,
    )

    urls = [item["image_url"] for item in client.get_recent_images(chat_id="leon-agent:test")["items"]]

    assert urls == [
        f"{PUBLIC_URL}/view?filename=a.png",
        f"{PUBLIC_URL}/view?filename=b.png",
        "https://cdn.example.com/view?filename=c.png",
        None,
        None,
    ]


@pytest.mark.parametrize(
    ("public_image_base_url", "expected"),
    [("", BACKEND_URL), (f"{PUBLIC_URL}/", PUBLIC_URL)],
)
def test_settings_public_base_url_falls_back_to_backend(
    monkeypatch: pytest.MonkeyPatch,
    public_image_base_url: str,
    expected: str,
) -> None:
    monkeypatch.setenv("LEON_BACKEND_URL", f"{BACKEND_URL}/")
    monkeypatch.setenv("LEON_PUBLIC_IMAGE_BASE_URL", public_image_base_url)

    settings = LeonSettings()

    assert settings.active_public_image_base_url == expected
