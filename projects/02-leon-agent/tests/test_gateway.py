"""Smoke tests for the Leon Agent HTTP Gateway."""

from __future__ import annotations

import asyncio
import importlib
import json
from threading import Event
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from leon_agent.gateway.app import _resolve_web_dir, _track_image_jobs, app, get_store
from leon_agent.gateway.events import EventBusRegistry
from leon_agent.session import SessionStore
from leon_agent.tools import create_leon_tools as build_leon_tools
from workbench_core.agent import (
    AgentCancelled,
    AgentEvent,
    AgentResult,
    ToolStep,
    TraceContext,
    TraceRecorder,
)

_FILE_TOOL_NAMES = frozenset(
    {"list_files", "file_search", "read_file", "create_file", "write_file"}
)
_MEMORY_TOOL_NAMES = frozenset({"memory_get", "memory_upsert", "memory_delete"})


@pytest.fixture()
def client(isolated_user_config):  # noqa: ARG001
    """Provide a TestClient with a temporary SQLite DB."""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _install_gateway_agent_capture(
    monkeypatch: pytest.MonkeyPatch,
    captures: list[dict[str, Any]],
) -> None:
    gateway_app = importlib.import_module("leon_agent.gateway.app")

    class FakeImageClient:
        def list_modes(self) -> dict[str, Any]:
            return {
                "ok": True,
                "modes": [
                    {"id": "k2_queen_marika"},
                    {"id": "k2_tifa_plus"},
                ],
            }

    class FakeLLMClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def close(self) -> None:
            pass

    class CapturingAgent:
        def __init__(self, **kwargs):  # noqa: ANN003
            file_service = kwargs.get("file_service")
            file_write_service = kwargs.get("file_write_service")
            memory_service = kwargs.get("memory_service")
            registry = build_leon_tools(
                kwargs["image_client"],
                session_id=kwargs["session_id"],
                default_mode_ids=kwargs["default_mode_ids"],
                file_service=file_service,
                file_write_service=file_write_service,
                memory_service=memory_service,
            )
            self.capture = {
                "file_service": file_service,
                "file_write_service": file_write_service,
                "file_tools": sorted(_FILE_TOOL_NAMES.intersection(registry.names)),
                "memory_service": memory_service,
                "memory_tools": sorted(_MEMORY_TOOL_NAMES.intersection(registry.names)),
            }
            captures.append(self.capture)

        def run(  # noqa: ANN001
            self,
            message,
            *,
            history=(),
            cancel_event=None,
            trace_context=None,
            trace_sink=None,
        ):
            del message, history, cancel_event
            self.capture["trace_context"] = trace_context
            self.capture["trace_sink"] = trace_sink
            return AgentResult(
                answer="captured",
                trace_id=trace_context.trace_id,
                turn_id=trace_context.turn_id,
            )

    fake_image_client = FakeImageClient()
    monkeypatch.setattr(gateway_app, "_create_image_client", lambda _config: fake_image_client)
    monkeypatch.setattr(gateway_app, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(gateway_app, "LeonAgent", CapturingAgent)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_health_requires_configured_token(isolated_user_config):
    isolated_user_config(LEON_API_TOKEN="test-token")
    with TestClient(app, raise_server_exceptions=True) as client:
        assert client.get("/api/health").status_code == 401
        assert client.get(
            "/api/health", headers={"Authorization": "Bearer test-token"}
        ).status_code == 200


def test_sse_invalid_token_stops_eventsource_retry(isolated_user_config):
    isolated_user_config(LEON_API_TOKEN="test-token")
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


def test_send_message_without_file_roots_injects_no_file_registry(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures: list[dict[str, Any]] = []
    _install_gateway_agent_capture(monkeypatch, captures)
    session_id = client.post("/api/agent/sessions").json()["session_id"]

    response = client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "hello"},
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "captured"
    assert len(captures) == 1
    assert captures[0]["file_service"] is None
    assert captures[0]["file_write_service"] is None
    assert captures[0]["file_tools"] == []
    context = captures[0]["trace_context"]
    assert context.session_id == session_id
    assert context.entrypoint == "web"
    gateway_app = importlib.import_module("leon_agent.gateway.app")
    assert captures[0]["trace_sink"] is gateway_app.get_trace_store()
    messages = client.get(f"/api/agent/sessions/{session_id}").json()["messages"]
    assert {message["turn_id"] for message in messages} == {context.turn_id}


def test_web_retry_reuses_turn_id_and_creates_a_new_trace(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures: list[dict[str, Any]] = []
    _install_gateway_agent_capture(monkeypatch, captures)
    gateway_app = importlib.import_module("leon_agent.gateway.app")
    session_id = client.post("/api/agent/sessions").json()["session_id"]

    first = client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "first"},
    )
    retried = client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "first edited", "retry": True},
    )

    assert first.status_code == retried.status_code == 200
    first_context = captures[0]["trace_context"]
    retry_context = captures[1]["trace_context"]
    assert retry_context.turn_id == first_context.turn_id
    assert retry_context.trace_id != first_context.trace_id
    messages = client.get(f"/api/agent/sessions/{session_id}").json()["messages"]
    assert [message["content"] for message in messages] == ["first edited", "captured"]
    assert {message["turn_id"] for message in messages} == {first_context.turn_id}

    bus = gateway_app._bus_registry.get(session_id)  # noqa: SLF001
    assert bus is not None
    retry_events = [
        event
        for event in bus._history  # noqa: SLF001
        if event.data.get("trace_id") == retry_context.trace_id
    ]
    assert {event.event for event in retry_events} >= {
        "user.message",
        "assistant.started",
        "assistant.completed",
    }
    assert {event.data["turn_id"] for event in retry_events} == {
        first_context.turn_id
    }


def test_trace_query_endpoints_enforce_session_ownership(client) -> None:
    gateway_app = importlib.import_module("leon_agent.gateway.app")
    session_id = client.post("/api/agent/sessions").json()["session_id"]
    other_session_id = client.post("/api/agent/sessions").json()["session_id"]
    context = TraceContext.create(session_id=session_id, entrypoint="web")
    recorder = TraceRecorder(context, gateway_app.get_trace_store())
    span_id = recorder.start_span(
        "llm",
        "llm.request",
        attributes={"message_count": 2, "prompt": "must-not-leak"},
    )
    recorder.finish_span(span_id, model="fake-model", input_tokens=3, output_tokens=2)
    recorder.finish_trace(status="ok", outcome="answered")

    listed = client.get(f"/api/agent/sessions/{session_id}/traces")
    detail = client.get(
        f"/api/agent/sessions/{session_id}/traces/{context.trace_id}"
    )
    denied = client.get(
        f"/api/agent/sessions/{other_session_id}/traces/{context.trace_id}"
    )

    assert listed.status_code == detail.status_code == 200
    assert listed.json()["traces"][0]["trace_id"] == context.trace_id
    assert detail.json()["trace"]["turn_id"] == context.turn_id
    assert [span["kind"] for span in detail.json()["spans"]] == ["agent", "llm"]
    assert detail.json()["spans"][1]["attributes"] == {"message_count": 2}
    assert "must-not-leak" not in repr(detail.json())
    assert denied.status_code == 404


def test_send_message_injects_memory_service_on_shared_database(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures: list[dict[str, Any]] = []
    _install_gateway_agent_capture(monkeypatch, captures)
    session_id = client.post("/api/agent/sessions").json()["session_id"]

    for content in ("first", "second"):
        response = client.post(
            f"/api/agent/sessions/{session_id}/messages",
            json={"content": content},
        )
        assert response.status_code == 200

    gateway_app = importlib.import_module("leon_agent.gateway.app")
    assert len(captures) == 2
    assert captures[0]["memory_tools"] == sorted(_MEMORY_TOOL_NAMES)
    assert captures[1]["memory_tools"] == sorted(_MEMORY_TOOL_NAMES)
    assert captures[0]["memory_service"] is not captures[1]["memory_service"]
    assert captures[0]["memory_service"].store is gateway_app.get_memory_store()
    assert captures[1]["memory_service"].store is gateway_app.get_memory_store()
    assert captures[0]["memory_service"].session_id == session_id


def test_send_message_builds_isolated_file_write_services_for_agent_and_direct_tools(
    tmp_path,
    isolated_user_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    isolated_user_config(
        LEON_FILE_ROOTS=json.dumps({"workbench": str(root)}),
    )
    captures: list[dict[str, Any]] = []
    _install_gateway_agent_capture(monkeypatch, captures)
    gateway_app = importlib.import_module("leon_agent.gateway.app")

    with TestClient(app, raise_server_exceptions=True) as configured_client:
        session_id = configured_client.post("/api/agent/sessions").json()["session_id"]
        for content in ("first turn", "second turn"):
            response = configured_client.post(
                f"/api/agent/sessions/{session_id}/messages",
                json={"content": content},
            )
            assert response.status_code == 200

        direct_captures: list[dict[str, Any]] = []

        class FakeDirectTools:
            def execute(self, name, arguments):  # noqa: ANN001
                assert name == "generate_images"
                assert arguments["source_text"] == "direct turn"
                return {"ok": True, "generation_plan_id": "probe", "jobs": []}

        def capture_direct_tools(_client, **kwargs):  # noqa: ANN001, ANN003
            direct_captures.append(kwargs)
            return FakeDirectTools()

        monkeypatch.setattr(gateway_app, "create_leon_tools", capture_direct_tools)
        direct_response = configured_client.post(
            f"/api/agent/sessions/{session_id}/messages",
            json={"content": "/NSFW --model tifa-plus direct turn"},
        )

    assert direct_response.status_code == 200
    assert len(captures) == 2
    for capture in captures:
        assert capture["file_tools"] == sorted(_FILE_TOOL_NAMES)
        assert capture["file_service"].root_bindings == (
            capture["file_write_service"].root_bindings
        )
    assert captures[0]["file_write_service"] is not captures[1]["file_write_service"]
    assert len(direct_captures) == 1
    direct = direct_captures[0]
    assert direct["file_service"].root_bindings == direct["file_write_service"].root_bindings
    assert direct["file_write_service"] is not captures[1]["file_write_service"]


def test_cancelled_side_effect_persists_only_projected_tool_audit(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures: list[dict[str, Any]] = []
    _install_gateway_agent_capture(monkeypatch, captures)
    gateway_app = importlib.import_module("leon_agent.gateway.app")
    marker = "raw-file-content-must-not-persist"

    class CancellingAgent:
        def __init__(self, **kwargs):  # noqa: ANN003
            del kwargs

        def run(  # noqa: ANN001
            self,
            message,
            *,
            history=(),
            cancel_event=None,
            trace_context=None,
            trace_sink=None,
        ):
            del message, history, cancel_event, trace_sink
            raise AgentCancelled(
                partial_result=AgentResult(
                    answer=marker,
                    messages=[{"role": "tool", "content": marker}],
                    steps=[
                        ToolStep(
                            "create_file",
                            {"root_id": "workbench", "relative_path": "note.md"},
                            {
                                "ok": True,
                                "created": True,
                                "root_id": "workbench",
                                "path": "note.md",
                                "citation": "workbench:note.md",
                                "bytes": 4,
                            },
                            trace_id=trace_context.trace_id,
                            span_id="a" * 16,
                        )
                    ],
                    trace_id=trace_context.trace_id,
                    turn_id=trace_context.turn_id,
                )
            )

    monkeypatch.setattr(gateway_app, "LeonAgent", CancellingAgent)
    session_id = client.post("/api/agent/sessions").json()["session_id"]

    response = client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "cancel after side effect"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "session_id": session_id,
        "answer": "已停止",
        "ok": False,
    }
    store = get_store()
    with store._connect() as connection:  # noqa: SLF001 - verify persistence boundary
        rows = connection.execute(
            """
            SELECT name, arguments_json, result_json
            FROM tool_calls WHERE session_id = ? ORDER BY id
            """,
            (session_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "create_file"
    assert json.loads(rows[0]["arguments_json"]) == {
        "root_id": "workbench",
        "relative_path": "note.md",
    }
    assert json.loads(rows[0]["result_json"])["created"] is True
    assert marker not in repr([dict(row) for row in rows])
    assert store.load_messages(session_id) == [
        {"role": "user", "content": "cancel after side effect"}
    ]


def test_cancelled_stream_persists_one_partial_assistant_message(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_app = importlib.import_module("leon_agent.gateway.app")

    class StreamingCancellingAgent:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.on_event = kwargs["on_event"]

        def run(  # noqa: ANN001
            self,
            message,
            *,
            history=(),
            cancel_event=None,
            trace_context=None,
            trace_sink=None,
        ):
            del message, history, cancel_event, trace_sink
            for content in ("已经输出的", "部分回答"):
                self.on_event(
                    AgentEvent(
                        kind="assistant_delta",
                        content=content,
                        trace_id=trace_context.trace_id,
                        turn_id=trace_context.turn_id,
                    )
                )
            raise AgentCancelled(
                partial_result=AgentResult(
                    answer="",
                    trace_id=trace_context.trace_id,
                    turn_id=trace_context.turn_id,
                )
            )

    monkeypatch.setattr(gateway_app, "LeonAgent", StreamingCancellingAgent)
    monkeypatch.setattr(gateway_app, "LLMClient", lambda *args, **kwargs: SimpleNamespace())
    session_id = client.post("/api/agent/sessions").json()["session_id"]

    response = client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "给一个很长的回答"},
    )
    restored = client.get(f"/api/agent/sessions/{session_id}").json()["messages"]

    assert response.status_code == 200
    assert response.json()["answer"] == "已停止"
    assert [message["content"] for message in restored] == [
        "给一个很长的回答",
        "已经输出的部分回答",
    ]
    assert restored[0]["turn_id"] == restored[1]["turn_id"]
    bus = gateway_app._bus_registry.get(session_id)  # noqa: SLF001
    assert bus is not None
    cancelled = next(
        event for event in reversed(bus._history) if event.event == "assistant.cancelled"  # noqa: SLF001
    )
    assert cancelled.data["content"] == "已经输出的部分回答"


def test_cancel_endpoint_keeps_the_active_turn_until_its_worker_finishes(client):
    gateway_app = importlib.import_module("leon_agent.gateway.app")
    session_id = client.post("/api/agent/sessions").json()["session_id"]
    cancel_event = Event()
    closed: list[str] = []

    class FakeLLMClient:
        def close(self) -> None:
            closed.append("closed")

    gateway_app._active_turns[session_id] = gateway_app.ActiveTurnState(
        cancel_event=cancel_event,
        retry_latest=True,
        llm_client=FakeLLMClient(),
    )

    session = client.get(f"/api/agent/sessions/{session_id}")
    messages = client.get(f"/api/agent/sessions/{session_id}/messages")

    response = client.post(f"/api/agent/sessions/{session_id}/cancel")

    assert session.json()["active_turn"] == {"retry": True}
    assert messages.json()["active_turn"] == {"retry": True}
    assert response.status_code == 200
    assert response.json() == {"session_id": session_id, "cancelled": True}
    assert cancel_event.is_set()
    assert closed == ["closed"]
    assert session_id in gateway_app._active_turns
    assert (
        client.post(
            f"/api/agent/sessions/{session_id}/messages",
            json={"content": "must wait for the cancelled worker"},
        ).status_code
        == 409
    )
    gateway_app._active_turns.pop(session_id)

    delete_event = Event()
    gateway_app._active_turns[session_id] = gateway_app.ActiveTurnState(
        cancel_event=delete_event
    )
    delete_response = client.delete(f"/api/agent/sessions/{session_id}/cancel")

    assert delete_response.status_code == 200
    assert delete_response.json()["cancelled"] is True
    assert delete_event.is_set()
    gateway_app._active_turns.pop(session_id)


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


def test_voice_clip_route_falls_back_to_sqlite_after_memory_cache_miss(client):
    gateway_app = importlib.import_module("leon_agent.gateway.app")
    session_id = client.post("/api/agent/sessions").json()["session_id"]
    clip = get_store().add_voice_clip(
        session_id,
        text="刷新后仍可播放",
        voice_id="voice-persisted",
        voice_name="持久化音色",
        audio=b"persistent-fake-mp3",
    )
    gateway_app._voice_clips = gateway_app.VoiceClipStore()  # noqa: SLF001

    session = client.get(f"/api/agent/sessions/{session_id}").json()
    assert session["voice_clips"] == [
        {
            **clip,
            "url": f"/api/voice/clips/{clip['clip_id']}",
        }
    ]

    response = client.get(f"/api/voice/clips/{clip['clip_id']}")
    assert response.status_code == 200
    assert response.content == b"persistent-fake-mp3"
    assert response.headers["content-type"] == "audio/mpeg"


def test_session_speak_uses_turn_voice_unless_tool_explicitly_overrides(
    tmp_path,
    monkeypatch,
):
    gateway_app = importlib.import_module("leon_agent.gateway.app")
    calls: list[tuple[str, str]] = []

    class FakeVoiceClient:
        def synthesize(self, *, text: str, voice_id: str) -> bytes:
            calls.append((text, voice_id))
            return f"audio:{voice_id}:{text}".encode()

        def resolve_voice(self, voice_id: str) -> dict[str, str]:
            return {"name": f"name:{voice_id}"}

    monkeypatch.setattr(gateway_app, "_get_voice_client", lambda config: FakeVoiceClient())
    gateway_app._tts_audio_cache = gateway_app.TTSAudioCache(max_count=10)  # noqa: SLF001
    store = SessionStore(tmp_path / "voice-handler.db")
    session_id = store.create_session()
    events = []
    handler = gateway_app._session_speak_factory(  # noqa: SLF001
        SimpleNamespace(voice_enabled=True, volink_default_voice_id="voice-default"),
        session_id,
        events.append,
        store=store,
        preferred_voice_id="voice-selected",
    )
    assert handler is not None

    handler("使用页面选择", None)
    handler("使用显式音色", "voice-explicit")

    assert calls == [
        ("使用页面选择", "voice-selected"),
        ("使用显式音色", "voice-explicit"),
    ]
    assert [clip["voice_id"] for clip in store.load_voice_clips(session_id)] == [
        "voice-selected",
        "voice-explicit",
    ]
    assert [event.data["voice_id"] for event in events] == [
        "voice-selected",
        "voice-explicit",
    ]


def test_asr_status_and_transcription_are_disabled_without_configuration(
    isolated_user_config,
):
    isolated_user_config(LEON_ASR_BASE_URL="", LEON_ASR_TOKEN="")
    with TestClient(app, raise_server_exceptions=True) as test_client:
        assert test_client.get("/api/agent/asr/status").json() == {"enabled": False}
        response = test_client.post(
            "/api/agent/asr",
            files={"audio": ("sample.webm", b"audio", "audio/webm")},
        )

    assert response.status_code == 503
    assert "ASR 未配置" in response.json()["detail"]


def test_asr_transcription_forwards_audio_and_model(isolated_user_config, monkeypatch):
    gateway_app = importlib.import_module("leon_agent.gateway.app")
    isolated_user_config(
        LEON_ASR_BASE_URL="https://asr.example/v1/",
        LEON_ASR_TOKEN="asr-test-token",
        LEON_ASR_MODEL="whisper-test",
    )
    calls = {}

    class FakeAsyncClient:
        def __init__(self, *, timeout):  # noqa: ANN001
            calls["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):  # noqa: ANN001
            return False

        async def post(self, url, **kwargs):  # noqa: ANN001, ANN003
            calls["url"] = url
            calls["kwargs"] = kwargs
            return httpx.Response(200, json={"text": "  转写成功  "})

    monkeypatch.setattr(gateway_app.httpx, "AsyncClient", FakeAsyncClient)

    with TestClient(app, raise_server_exceptions=True) as test_client:
        assert test_client.get("/api/agent/asr/status").json() == {"enabled": True}
        response = test_client.post(
            "/api/agent/asr",
            files={"audio": ("sample.webm", b"audio-bytes", "audio/webm")},
        )

    assert response.status_code == 200
    assert response.json() == {"text": "转写成功"}
    assert calls["url"] == "https://asr.example/v1/audio/transcriptions"
    assert calls["kwargs"]["headers"] == {"Authorization": "Bearer asr-test-token"}
    assert calls["kwargs"]["data"] == {"model": "whisper-test"}
    assert calls["kwargs"]["files"] == {
        "file": ("sample.webm", b"audio-bytes", "audio/webm")
    }


def test_asr_rejects_empty_and_oversized_audio_before_upstream(
    isolated_user_config, monkeypatch
):
    gateway_app = importlib.import_module("leon_agent.gateway.app")
    isolated_user_config(
        LEON_ASR_BASE_URL="https://asr.example/v1",
        LEON_ASR_TOKEN="asr-test-token",
        LEON_ASR_MAX_BYTES="4",
    )

    class UnexpectedAsyncClient:
        def __init__(self, **kwargs):  # noqa: ANN003
            raise AssertionError("oversized or empty audio must not call ASR upstream")

    monkeypatch.setattr(gateway_app.httpx, "AsyncClient", UnexpectedAsyncClient)

    with TestClient(app, raise_server_exceptions=True) as test_client:
        empty = test_client.post(
            "/api/agent/asr",
            files={"audio": ("empty.webm", b"", "audio/webm")},
        )
        oversized = test_client.post(
            "/api/agent/asr",
            files={"audio": ("large.webm", b"12345", "audio/webm")},
        )

    assert empty.status_code == 400
    assert oversized.status_code == 413


def test_asr_maps_upstream_transport_failure_to_bad_gateway(
    isolated_user_config, monkeypatch
):
    gateway_app = importlib.import_module("leon_agent.gateway.app")
    isolated_user_config(
        LEON_ASR_BASE_URL="https://asr.example/v1",
        LEON_ASR_TOKEN="asr-test-token",
    )

    class FailingAsyncClient:
        def __init__(self, **kwargs):  # noqa: ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):  # noqa: ANN001
            return False

        async def post(self, url, **kwargs):  # noqa: ANN001, ANN003
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(gateway_app.httpx, "AsyncClient", FailingAsyncClient)

    with TestClient(app, raise_server_exceptions=True) as test_client:
        response = test_client.post(
            "/api/agent/asr",
            files={"audio": ("sample.webm", b"audio", "audio/webm")},
        )

    assert response.status_code == 502
    assert "ASR 服务请求失败" in response.json()["detail"]


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
