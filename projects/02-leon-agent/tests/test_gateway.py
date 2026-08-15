"""Smoke tests for the Leon Agent HTTP Gateway."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from leon_agent.gateway.app import _track_image_jobs, app, get_store
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
    assert client.get("/manifest.json").status_code == 200
    assert client.get("/icon.svg").status_code == 200


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
                        "image_url": "https://images.example/task.png",
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
    assert {item["job_id"] for item in data["images"]} == {
        "job-from-task",
        "job-from-gallery",
    }
    assert ("tasks", f"leon-agent:{session_id}", 25) in calls
    assert ("gallery", f"leon-agent:{session_id}", 25) in calls


def test_web_client_supports_markdown_images_and_touch_scrolling(client):
    html = client.get("/").text

    assert 'class="markdown-image-link"' in html
    assert "function isImageHref" in html
    assert "url.searchParams.get('filename')" in html
    assert 'alt="生成图片"' in html
    assert 'id="image-viewer"' in html
    assert "openImageViewer" in html
    assert "viewerPointers" in html
    assert "image-viewer-zoom-in" not in html
    assert "replaceSkeletonWithImage" in html
    assert 'id="mode-suggestions"' in html
    assert "/api/image-modes" in html
    assert "touch-action:pan-y" in html
    assert "autoFollowMessages" in html
    assert "/image-state?limit=100" in html
    assert "localStorage.removeItem(SESSION_KEY)" in html
    assert "maximum-scale=1,user-scalable=no" in html
    assert "interactive-widget=resizes-content" in html
    assert "window.visualViewport" not in html
    assert "window.open(href" not in html
    assert "$input.addEventListener('focus'" not in html
    assert "height:100dvh" in html
    assert "font-size:16px" in html
    assert "/sw.js?v=15" in html


def test_web_client_image_viewer_is_a_zoomable_album(client):
    html = client.get("/").text

    # Every control needs an explicit z-index: the <img> has will-change:transform,
    # so it creates a stacking context that paints over z-index:auto siblings.
    assert ".iv-chrome{position:absolute;z-index:3}" in html
    assert 'id="image-viewer-close" class="iv-chrome"' in html
    # Focal-point zoom replaced the old centre-only setViewerScale().
    assert "function zoomAt(" in html
    assert "setViewerScale" not in html
    # Pan bounds clamp to the real image edges, no arbitrary slack.
    assert "+48" not in html
    # Album navigation with wrap-around.
    assert 'id="image-viewer-prev"' in html
    assert 'id="image-viewer-next"' in html
    assert 'id="image-viewer-counter"' in html
    assert "function collectAlbum(" in html
    assert "ivIndex=(index%ivAlbum.length+ivAlbum.length)%ivAlbum.length" in html


def test_web_client_appends_finished_image_as_new_bubble(client):
    html = client.get("/").text

    # The skeleton is dropped and the image is appended at the bottom, instead of
    # being swapped in place halfway up the conversation.
    assert "function replaceSkeletonWithImage(" in html
    assert "imageJobMessages" not in html
    assert "addImageSkeleton" not in html
    assert "updateSkeletonBadge" not in html
    assert "createMessage({kind:'image',status:'done',images:[href]});" in html
    assert "placeholder.replaceWith" not in html


def test_web_client_renders_every_bubble_from_the_message_store(client):
    html = client.get("/").text

    # W1: one message list is the only source of truth; renderers read from it and
    # patch a single bubble instead of appending straight into the DOM.
    assert "const messages=[],messageIndex=new Map();" in html
    assert "function createMessage(" in html
    assert "function renderMessage(" in html
    assert "function renderBubbleBody(" in html
    assert "function renderBubbleToolbar(" in html
    assert "function patchMessage(" in html
    assert "wrap.dataset.messageId=msg.id;" in html
    assert "data-message-id=" in html
    assert "class=\"bubble-toolbar\"" not in html
    assert "bar.className='bubble-toolbar';" in html
    # The old ad-hoc DOM registry is gone.
    assert "imagePlaceholders" not in html


def test_web_client_has_complete_bubble_actions_and_voice_states(client):
    html = client.get("/").text

    assert "ICON_COPY" in html
    assert "ICON_RETRY" in html
    assert "ICON_EDIT" in html
    assert "function startEditingMessage(" in html
    assert "function retryMessage(" in html
    assert "function speakableText(" in html
    assert "button.innerHTML=ICON_LOADING" in html
    assert "button.innerHTML=ICON_WAVE" in html
    assert "pendingVoice={url,onState}" in html
    assert "playVoice(waiting.url,{onState:waiting.onState})" in html


def test_web_client_model_picker_collapses_after_save(client):
    html = client.get("/").text

    assert "let modelListOpen=false;" in html
    assert "if(!modelListOpen)return;" in html
    assert "modelListOpen=false;renderModelList();" in html


def test_web_client_model_picker_is_tappable(client):
    html = client.get("/").text

    # <datalist> has no usable dropdown on mobile browsers.
    assert "<datalist" not in html
    assert 'list="model-options"' not in html
    assert 'id="model-list"' in html
    assert "function renderModelList(" in html
    assert "model-option" in html


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


def test_web_gallery_page_is_hidden_when_inactive(client):
    html = client.get("/").text

    assert "#page-gallery{padding:12px;display:flex" not in html


def test_send_message_session_not_found(client):
    r = client.post(
        "/api/agent/sessions/ghost/messages",
        json={"content": "hello"},
    )
    assert r.status_code == 404


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
        "answer": "已使用 蒂法增强 模式提交生图任务，完成后会自动在聊天里显示图片。",
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
            "content": "![生成图片 1](https://images.example/task-job-1.png)",
        },
        {
            "role": "assistant",
            "content": "这张图做好了，点开看看。",
        }
    ]
