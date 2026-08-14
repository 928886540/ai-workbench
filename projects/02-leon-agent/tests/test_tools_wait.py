from leon_agent.tools import _wait_for_image_results


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
