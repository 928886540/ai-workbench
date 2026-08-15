import threading
import time

import pytest
from leon_agent.tools import _wait_for_image_results
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
