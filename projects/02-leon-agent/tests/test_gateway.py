"""Smoke tests for the Leon Agent HTTP Gateway."""

from __future__ import annotations

import asyncio
import importlib
from threading import Event

import pytest
from fastapi.testclient import TestClient
from leon_agent.gateway.app import _resolve_web_dir, _track_image_jobs, app, get_store
from leon_agent.gateway.events import EventBusRegistry
from leon_agent.session import SessionStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Provide a TestClient with a temporary SQLite DB."""
    monkeypatch.setenv("LEON_SESSION_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("LEON_API_TOKEN", "")  # disable auth in tests
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_health_requires_configured_token(tmp_path, monkeypatch):
    monkeypatch.setenv("LEON_SESSION_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("LEON_API_TOKEN", "test-token")
    with TestClient(app, raise_server_exceptions=True) as client:
        assert client.get("/api/health").status_code == 401
        assert client.get(
            "/api/health", headers={"Authorization": "Bearer test-token"}
        ).status_code == 200


def test_sse_invalid_token_stops_eventsource_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("LEON_SESSION_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("LEON_API_TOKEN", "test-token")
    with TestClient(app, raise_server_exceptions=True) as client:
        session_id = client.post(
            "/api/agent/sessions", headers={"Authorization": "Bearer test-token"}
        ).json()["session_id"]
        response = client.get(
            f"/api/agent/sessions/{session_id}/events?token=stale-token"
        )
        assert response.status_code == 204


def test_web_manifest_and_icon_are_served(client):
    html = client.get("/").text
    assert '<div id="app"></div>' in html
    assert "/assets/index-" in html
    assert client.get("/manifest.json").status_code == 200
    assert client.get("/icon.svg").status_code == 200


def test_vue_web_dir_requires_a_built_entrypoint(tmp_path):
    vue_dir = tmp_path / "vue" / "dist"
    vue_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="Vue client build required"):
        _resolve_web_dir(vue_dist_dir=vue_dir)

    (vue_dir / "index.html").write_text("<div id='app'></div>", encoding="utf-8")
    assert _resolve_web_dir(vue_dist_dir=vue_dir) == vue_dir


def test_web_shell_disables_stale_pwa_cache(client):
    assert client.get("/").headers["cache-control"] == "no-cache, no-store, must-revalidate"
    sw = client.get("/sw.js")
    assert sw.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert sw.headers["service-worker-allowed"] == "/"


def test_create_and_list_session(client):
    r = client.post("/api/agent/sessions")
    assert r.status_code == 200
    data = r.json()
    assert "session_id" in data
    session_id = data["session_id"]

    r2 = client.get("/api/agent/sessions")
    assert r2.status_code == 200
    ids = [s["id"] for s in r2.json()["sessions"]]
    assert session_id in ids


def test_get_session_not_found(client):
    r = client.get("/api/agent/sessions/nonexistent")
    assert r.status_code == 404


def test_get_messages_empty(client):
    r = client.post("/api/agent/sessions")
    session_id = r.json()["session_id"]
    r2 = client.get(f"/api/agent/sessions/{session_id}/messages")
    assert r2.status_code == 200
    assert r2.json()["messages"] == []


def test_session_image_state_restores_tasks_and_gallery(client, monkeypatch):
    calls = []

    class FakeImageClient:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.kwargs = kwargs

        def get_image_tasks(self, *, chat_id, limit):  # noqa: ANN001
            calls.append(("tasks", chat_id, limit))
            return {
                "items": [
                    {
                        "job_id": "job-from-task",
                        "status": "completed",
                        "workflow_name": "k2_queen_marika",
                        "image_url": "https://images.example/task.png",
                        "created_at": "1770000000100",
                    }
                ]
            }

        def get_recent_images(self, *, chat_id, limit):  # noqa: ANN001
            calls.append(("gallery", chat_id, limit))
            return {
                "items": [
                    {
                        "job_id": "job-from-gallery",
                        "image_url": "https://images.example/gallery.png",
                        "created_at": "1770000000200",
                    }
                ]
            }

    monkeypatch.setattr("leon_agent.gateway.app.LeonImageClient", FakeImageClient)
    session_id = client.post("/api/agent/sessions").json()["session_id"]

    response = client.get(f"/api/agent/sessions/{session_id}/image-state?limit=25")

    assert response.status_code == 200
    data = response.json()
    assert data["errors"] == {}
    assert {item["job_id"] for item in data["tasks"]} == {"job-from-task"}
    assert data["tasks"][0]["mode_id"] == "k2_queen_marika"
    assert data["tasks"][0]["mode_name"] == "玛莉卡"
    assert data["images"][0]["job_id"] == "job-from-gallery"
    assert data["images"][1]["job_id"] == "job-from-task"
    assert {item["job_id"] for item in data["images"]} == {
        "job-from-task",
        "job-from-gallery",
    }
    assert ("tasks", f"leon-agent:{session_id}", 25) in calls
    assert ("gallery", f"leon-agent:{session_id}", 25) in calls


def test_session_model_can_be_selected_and_reset(client, monkeypatch):
    class FakeSettings:
        profile = "toml:test-provider"
        active_model = "default-model"
        active_base_url = "https://provider.example/v1"

    class FakeLLMClient:
        def __init__(self, settings):  # noqa: ANN001
            self.settings = settings

        def list_models(self):
            return ["Provider-Model-A", "Provider-Model-B"]

    monkeypatch.setattr("leon_agent.gateway.app.get_settings", lambda: FakeSettings())
    monkeypatch.setattr("leon_agent.gateway.app.LLMClient", FakeLLMClient)
    session_id = client.post("/api/agent/sessions").json()["session_id"]

    initial = client.get(f"/api/agent/sessions/{session_id}/model")
    assert initial.status_code == 200
    assert initial.json()["active_model"] == "default-model"
    assert initial.json()["selected_model"] is None
    assert initial.json()["models"] == [
        "Provider-Model-A",
        "Provider-Model-B",
        "default-model",
    ]

    selected = client.put(
        f"/api/agent/sessions/{session_id}/model",
        json={"model": "gpt-5.6-sol"},
    )
    assert selected.status_code == 200
    assert selected.json()["active_model"] == "gpt-5.6-sol"
    assert selected.json()["selected_model"] == "gpt-5.6-sol"

    reset = client.put(
        f"/api/agent/sessions/{session_id}/model",
        json={"model": None},
    )
    assert reset.status_code == 200
    assert reset.json()["active_model"] == "default-model"
    assert reset.json()["selected_model"] is None


def test_session_model_accepts_custom_case_sensitive_id(client, monkeypatch):
    class FakeSettings:
        profile = "toml:test-provider"
        active_model = "default-model"
        active_base_url = "https://provider.example/v1"

    class FakeLLMClient:
        def __init__(self, settings):  # noqa: ANN001
            self.settings = settings

        def list_models(self):
            return ["DeepSeek-V4-Flash-0731"]

    monkeypatch.setattr("leon_agent.gateway.app.get_settings", lambda: FakeSettings())
    monkeypatch.setattr("leon_agent.gateway.app.LLMClient", FakeLLMClient)
    session_id = client.post("/api/agent/sessions").json()["session_id"]
    response = client.put(
        f"/api/agent/sessions/{session_id}/model",
        json={"model": "Custom-Model-ID"},
    )
    assert response.status_code == 200
    assert response.json()["selected_model"] == "Custom-Model-ID"


def test_session_model_resets_when_provider_endpoint_changes(client, monkeypatch):
    settings = type(
        "FakeSettings",
        (),
        {
            "profile": "toml:codex",
            "active_model": "Provider-B-Default",
            "active_base_url": "https://provider-b.example/v1",
        },
    )()

    class FakeLLMClient:
        def __init__(self, value):  # noqa: ANN001
            self.settings = value

        def list_models(self):
            return ["Provider-B-Default"]

    monkeypatch.setattr("leon_agent.gateway.app.get_settings", lambda: settings)
    monkeypatch.setattr("leon_agent.gateway.app.LLMClient", FakeLLMClient)
    session_id = client.post("/api/agent/sessions").json()["session_id"]
    get_store().set_model_selection(
        session_id,
        provider="toml:codex|https://provider-a.example/v1",
        model="Provider-A-Model",
    )

    response = client.get(f"/api/agent/sessions/{session_id}/model")

    assert response.status_code == 200
    assert response.json()["selected_model"] is None
    assert response.json()["active_model"] == "Provider-B-Default"


def test_web_session_pins_complete_toml_provider_until_new_login(client, monkeypatch):
    class FakeSettings:
        def __init__(self, name):
            self.profile = f"toml:{name}"
            self.active_model = f"{name}-model"
            self.active_base_url = f"https://{name}.example/v1"

    active = {"settings": FakeSettings("provider-a")}

    class FakeLLMClient:
        def __init__(self, settings):  # noqa: ANN001
            self.settings = settings

        def list_models(self):
            return [self.settings.active_model]

    monkeypatch.setattr(
        "leon_agent.gateway.app.get_settings", lambda: active["settings"]
    )
    monkeypatch.setattr("leon_agent.gateway.app.LLMClient", FakeLLMClient)

    first_session = client.post("/api/agent/sessions").json()["session_id"]
    active["settings"] = FakeSettings("provider-b")

    pinned = client.get(f"/api/agent/sessions/{first_session}/model")
    second_session = client.post("/api/agent/sessions").json()["session_id"]
    refreshed = client.get(f"/api/agent/sessions/{second_session}/model")

    assert pinned.json()["base_url"] == "https://provider-a.example/v1"
    assert pinned.json()["default_model"] == "provider-a-model"
    assert refreshed.json()["base_url"] == "https://provider-b.example/v1"
    assert refreshed.json()["default_model"] == "provider-b-model"


def test_send_message_session_not_found(client):
    r = client.post(
        "/api/agent/sessions/ghost/messages",
        json={"content": "hello"},
    )
    assert r.status_code == 404


def test_cancel_endpoint_sets_and_releases_the_active_turn(client):
    gateway_app = importlib.import_module("leon_agent.gateway.app")
    session_id = client.post("/api/agent/sessions").json()["session_id"]
    cancel_event = Event()
    gateway_app._active_turns[session_id] = gateway_app.ActiveTurnState(
        cancel_event=cancel_event,
        retry_latest=True,
    )

    session = client.get(f"/api/agent/sessions/{session_id}")
    messages = client.get(f"/api/agent/sessions/{session_id}/messages")

    response = client.post(f"/api/agent/sessions/{session_id}/cancel")

    assert session.json()["active_turn"] == {"retry": True}
    assert messages.json()["active_turn"] == {"retry": True}
    assert response.status_code == 200
    assert response.json() == {"session_id": session_id, "cancelled": True}
    assert cancel_event.is_set()
    assert session_id not in gateway_app._active_turns

    delete_event = Event()
    gateway_app._active_turns[session_id] = gateway_app.ActiveTurnState(
        cancel_event=delete_event
    )
    delete_response = client.delete(f"/api/agent/sessions/{session_id}/cancel")

    assert delete_response.status_code == 200
    assert delete_response.json()["cancelled"] is True
    assert delete_event.is_set()


def test_tts_audio_cache_reuses_results_and_keeps_at_least_ten_entries():
    gateway_app = importlib.import_module("leon_agent.gateway.app")
    cache = gateway_app.TTSAudioCache(max_count=2)
    calls: list[str] = []

    def create(label: str) -> bytes:
        calls.append(label)
        return label.encode()

    first, first_hit = cache.get_or_create(
        text="同一句话",
        voice_id="voice-a",
        factory=lambda: create("first"),
    )
    reused, reused_hit = cache.get_or_create(
        text="同一句话",
        voice_id="voice-a",
        factory=lambda: create("unexpected"),
    )

    assert cache.max_count == 10
    assert first == reused == b"first"
    assert first_hit is False
    assert reused_hit is True
    assert calls == ["first"]

    for index in range(10):
        cache.get_or_create(
            text=f"其他文本 {index}",
            voice_id="voice-a",
            factory=lambda index=index: create(f"extra-{index}"),
        )

    recreated, recreated_hit = cache.get_or_create(
        text="同一句话",
        voice_id="voice-a",
        factory=lambda: create("recreated"),
    )
    assert recreated == b"recreated"
    assert recreated_hit is False


def test_image_modes_endpoint_returns_chinese_names_and_marika_default(client, monkeypatch):
    class FakeImageClient:
        def list_modes(self):
            return {
                "ok": True,
                "modes": [
                    {"id": "k2_queen_marika"},
                    {"id": "k2_tifa_plus"},
                ],
            }

    monkeypatch.setattr(
        "leon_agent.gateway.app._create_image_client", lambda config: FakeImageClient()
    )

    response = client.get("/api/image-modes")

    assert response.status_code == 200
    assert response.json()["default_mode_id"] == "k2_queen_marika"
    assert response.json()["default_mode_name"] == "玛莉卡"
    assert response.json()["modes"][1]["name"] == "蒂法增强"


def test_nsfw_message_bypasses_llm_and_resolves_selected_mode(client, monkeypatch):
    generate_calls = []

    class FakeImageClient:
        def list_modes(self):
            return {
                "ok": True,
                "modes": [
                    {"id": "k2_queen_marika"},
                    {"id": "k2_tifa_plus"},
                ],
            }

        def check_environment(self):
            return {"ok": True}

        def generate_images(self, **kwargs):  # noqa: ANN003
            generate_calls.append(kwargs)
            return {
                "ok": True,
                "generation_plan_id": "plan-nsfw",
                "jobs": [{"job_id": "job-nsfw", "status": "queued"}],
            }

        def get_image_tasks(self, **kwargs):  # noqa: ANN003
            return {"ok": True, "items": [{"job_id": "job-nsfw", "status": "failed"}]}

        def get_recent_images(self, **kwargs):  # noqa: ANN003
            return {"ok": True, "items": []}

        def get_latest_images(self, *, limit):  # noqa: ANN001
            return {"ok": True, "items": []}

    class ExplodingLLMClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("NSFW command must not construct an LLM client")

    fake_image_client = FakeImageClient()
    monkeypatch.setattr(
        "leon_agent.gateway.app._create_image_client", lambda config: fake_image_client
    )
    monkeypatch.setattr("leon_agent.gateway.app.LLMClient", ExplodingLLMClient)
    session_id = client.post("/api/agent/sessions").json()["session_id"]

    response = client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "/NSFW --model tifa-plus 原样描述"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "session_id": session_id,
        "answer": (
            "已使用 蒂法增强 模式提交 1 张图片任务，正在后台生成，请稍等；"
            "完成后会自动显示在这里。"
        ),
        "ok": True,
    }
    assert generate_calls[0]["source_text"] == "原样描述"
    assert generate_calls[0]["workflow_ids"] == ["k2_tifa_plus"]
    assert generate_calls[0]["batch_count"] == 1


def test_track_image_jobs_uses_task_image_and_publishes_human_notice(tmp_path):  # noqa: ANN001
    async def scenario():
        session_id = "session-track"
        store = SessionStore(tmp_path / "track.db")
        created_session_id = store.create_session()
        session_id = created_session_id
        registry = EventBusRegistry()
        queue = registry.get_or_create(session_id, asyncio.get_running_loop()).subscribe()

        class FakeImageClient:
            def get_image_tasks(self, **kwargs):  # noqa: ANN003
                return {
                    "items": [
                        {
                            "job_id": "job-1",
                            "status": "completed",
                            "progress": 100,
                            "image_url": "https://images.example/task-job-1.png",
                        }
                    ]
                }

            def get_recent_images(self, **kwargs):  # noqa: ANN003
                raise AssertionError("task image_url should avoid the gallery fallback")

        await _track_image_jobs(
            bus=registry,
            session_id=session_id,
            store=store,
            image_client=FakeImageClient(),  # type: ignore[arg-type]
            chat_id=f"leon-agent:{session_id}",
            generation_plan_id="plan-1",
            jobs=[{"job_id": "job-1", "status": "queued"}],
            completion_message_factory=lambda count: "这张图做好了，点开看看。",
        )
        await asyncio.sleep(0)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        return events, store.load_messages(session_id)

    events, messages = asyncio.run(scenario())
    completed_events = [event for event in events if event.event == "image.completed"]
    notice_events = [event for event in events if event.event == "assistant.notice"]

    assert len(completed_events) == 1
    assert completed_events[0].data["image_url"] == "https://images.example/task-job-1.png"
    assert len(notice_events) == 1
    assert notice_events[0].data["job_ids"] == ["job-1"]
    assert messages == [
        {
            "role": "assistant",
            "content": (
                "![生成图片 1](https://images.example/task-job-1.png)\n\n"
                "这张图做好了，点开看看。"
            ),
        }
    ]
