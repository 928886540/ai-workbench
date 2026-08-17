import threading
import time

import pytest
from leon_agent.service import _wait_for_image_results
from workbench_core.agent.runtime import AgentCancelled


class FakeImageClient:
    def __init__(self) -> None:
        self.task_calls = 0

    def get_image_tasks(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201
        assert chat_id == "leon-agent:test"
        assert limit >= 20
        self.task_calls += 1
        status = "running" if self.task_calls == 1 else "completed"
        return {
            "ok": True,
            "items": [
                {
                    "job_id": "job-1",
                    "status": status,
                    "workflow_name": "k2_tifa",
                    "image_url": None,
                }
            ],
        }

    def get_recent_images(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201
        assert chat_id == "leon-agent:test"
        assert limit >= 20
        return {
            "ok": True,
            "items": [
                {
                    "job_id": "job-1",
                    "workflow_name": "k2_tifa",
                    "image_url": "https://example.test/view?filename=done.png",
                }
            ],
        }


def test_wait_for_image_results_returns_completed_image() -> None:
    client = FakeImageClient()
    submission = {
        "ok": True,
        "generation_plan_id": "plan-1",
        "jobs": [{"job_id": "job-1", "status": "queued"}],
    }

    result = _wait_for_image_results(
        client,  # type: ignore[arg-type]
        chat_id="leon-agent:test",
        submission=submission,
        timeout_seconds=1,
        poll_interval_seconds=0.001,
    )

    assert result["timed_out"] is False
    assert result["tasks"][0]["status"] == "completed"
    assert result["images"] == [
        {
            "job_id": "job-1",
            "workflow_name": "k2_tifa",
            "image_url": "https://example.test/view?filename=done.png",
        }
    ]


def test_wait_for_image_results_has_no_default_deadline() -> None:
    class EventuallyCompletedClient(FakeImageClient):
        def get_image_tasks(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201
            result = super().get_image_tasks(chat_id=chat_id, limit=limit)
            if self.task_calls == 1:
                result["items"][0]["status"] = "running"
            return result

    client = EventuallyCompletedClient()
    result = _wait_for_image_results(
        client,  # type: ignore[arg-type]
        chat_id="leon-agent:test",
        submission={"ok": True, "jobs": [{"job_id": "job-1"}]},
        poll_interval_seconds=0.001,
    )

    assert client.task_calls == 2
    assert result["timed_out"] is False
    assert result["images"][0]["image_url"].endswith("done.png")


def test_wait_for_image_results_uses_completed_task_url_without_gallery() -> None:
    class TaskImageClient:
        def get_image_tasks(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201, ARG002
            return {
                "ok": True,
                "items": [
                    {
                        "job_id": "job-1",
                        "status": "completed",
                        "workflow_name": "k2_queen_marika",
                        "final_image_url": " https://example.test/task.png ",
                    }
                ],
            }

        def get_recent_images(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201, ARG002
            raise AssertionError("a completed task URL should avoid the lagging gallery")

    result = _wait_for_image_results(
        TaskImageClient(),  # type: ignore[arg-type]
        chat_id="leon-agent:test",
        submission={"ok": True, "jobs": [{"job_id": "job-1"}]},
        timeout_seconds=1,
        poll_interval_seconds=0.001,
    )

    assert result["ok"] is True
    assert result["images"] == [
        {
            "job_id": "job-1",
            "status": "completed",
            "workflow_name": "k2_queen_marika",
            "final_image_url": " https://example.test/task.png ",
            "image_url": "https://example.test/task.png",
        }
    ]


def test_wait_for_image_results_merges_and_deduplicates_task_and_gallery_urls() -> None:
    class MixedImageClient:
        def get_image_tasks(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201, ARG002
            return {
                "ok": True,
                "items": [
                    {
                        "job_id": "job-1",
                        "status": "completed",
                        "imageUrl": "https://example.test/one.png",
                    },
                    {"job_id": "job-2", "status": "completed"},
                ],
            }

        def get_recent_images(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201, ARG002
            return {
                "ok": True,
                "items": [
                    {"job_id": "job-1", "image_url": "https://example.test/one.png"},
                    {"job_id": "job-2", "finalImageUrl": "https://example.test/two.png"},
                    {"job_id": "job-2", "image_url": "https://example.test/two.png"},
                ],
            }

    result = _wait_for_image_results(
        MixedImageClient(),  # type: ignore[arg-type]
        chat_id="leon-agent:test",
        submission={
            "ok": True,
            "jobs": [{"job_id": "job-1"}, {"job_id": "job-2"}],
        },
        timeout_seconds=1,
        poll_interval_seconds=0.001,
    )

    assert result["ok"] is True
    assert [item["image_url"] for item in result["images"]] == [
        "https://example.test/one.png",
        "https://example.test/two.png",
    ]


def test_wait_for_image_results_marks_completed_task_without_url_retryable() -> None:
    class MissingImageClient:
        def get_image_tasks(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201, ARG002
            return {
                "ok": True,
                "items": [{"job_id": "job-1", "status": "completed"}],
            }

        def get_recent_images(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201, ARG002
            return {"ok": True, "items": []}

    result = _wait_for_image_results(
        MissingImageClient(),  # type: ignore[arg-type]
        chat_id="leon-agent:test",
        submission={"ok": True, "jobs": [{"job_id": "job-1"}]},
        timeout_seconds=1,
        poll_interval_seconds=0.001,
    )

    assert result["ok"] is False
    assert result["submitted"] is True
    assert result["retryable"] is True
    assert result["error_code"] == "image_result_unavailable"
    assert result["missing_image_count"] == 1
    assert result["images"] == []


def test_wait_for_image_results_marks_terminal_backend_failure() -> None:
    class FailedImageClient:
        def get_image_tasks(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201, ARG002
            return {
                "ok": True,
                "items": [
                    {
                        "job_id": "job-1",
                        "status": "failed",
                        "error": "backend generation failed",
                    }
                ],
            }

        def get_recent_images(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201, ARG002
            return {
                "ok": True,
                "items": [{"job_id": "job-1", "image_url": None}],
            }

    result = _wait_for_image_results(
        FailedImageClient(),  # type: ignore[arg-type]
        chat_id="leon-agent:test",
        submission={"ok": True, "jobs": [{"job_id": "job-1"}]},
        timeout_seconds=1,
        poll_interval_seconds=0.001,
    )

    assert result["ok"] is False
    assert result["submitted"] is True
    assert result["failed_count"] == 1
    assert result["error"] == "backend generation failed"
    assert result["images"] == []


def test_wait_for_image_results_marks_terminal_cancellation() -> None:
    class CancelledImageClient:
        def get_image_tasks(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201, ARG002
            return {
                "ok": True,
                "items": [{"job_id": "job-1", "status": "cancelled"}],
            }

        def get_recent_images(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201, ARG002
            raise AssertionError("cancelled jobs must not query the gallery")

    result = _wait_for_image_results(
        CancelledImageClient(),  # type: ignore[arg-type]
        chat_id="leon-agent:test",
        submission={"ok": True, "jobs": [{"job_id": "job-1"}]},
        timeout_seconds=1,
        poll_interval_seconds=0.001,
    )

    assert result["ok"] is False
    assert result["cancelled_count"] == 1
    assert result["error"] == "图片任务已取消"
    assert result["images"] == []


def test_wait_for_image_results_stops_before_next_poll_when_cancelled() -> None:
    cancel_event = threading.Event()

    class CancelAfterFirstPoll(FakeImageClient):
        def get_image_tasks(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201
            result = super().get_image_tasks(chat_id=chat_id, limit=limit)
            cancel_event.set()
            return result

        def get_recent_images(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201
            raise AssertionError("cancelled polling must not fetch the gallery")

    submission = {
        "ok": True,
        "jobs": [{"job_id": "job-1", "status": "queued"}],
    }

    client = CancelAfterFirstPoll()
    with pytest.raises(AgentCancelled):
        _wait_for_image_results(
            client,  # type: ignore[arg-type]
            chat_id="leon-agent:test",
            submission=submission,
            timeout_seconds=10,
            poll_interval_seconds=5,
            cancel_event=cancel_event,
        )

    assert client.task_calls == 1


def test_wait_for_image_results_event_wait_wakes_without_sleeping_full_interval() -> None:
    cancel_event = threading.Event()
    first_poll = threading.Event()

    class RunningImageClient:
        def get_image_tasks(self, *, chat_id: str, limit: int = 20):  # noqa: ANN201, ARG002
            first_poll.set()
            return {"ok": True, "items": [{"job_id": "job-1", "status": "running"}]}

    def cancel() -> None:
        assert first_poll.wait(timeout=1)
        cancel_event.set()

    canceller = threading.Thread(target=cancel)
    canceller.start()
    started = time.monotonic()
    with pytest.raises(AgentCancelled):
        _wait_for_image_results(
            RunningImageClient(),  # type: ignore[arg-type]
            chat_id="leon-agent:test",
            submission={"jobs": [{"job_id": "job-1"}]},
            timeout_seconds=10,
            poll_interval_seconds=5,
            cancel_event=cancel_event,
        )
    elapsed = time.monotonic() - started
    canceller.join(timeout=1)

    assert elapsed < 1
