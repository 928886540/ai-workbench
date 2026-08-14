"""Leon Agent HTTP Gateway — FastAPI application.

Phase 1 scope:
  POST   /api/agent/sessions                          create session
  GET    /api/agent/sessions                          list sessions
  GET    /api/agent/sessions/{id}                     session detail + history
  GET    /api/agent/sessions/{id}/messages            message history
  GET    /api/agent/sessions/{id}/image-state         image tasks + gallery
  POST   /api/agent/sessions/{id}/messages            send message (runs Agent)
  GET    /api/agent/sessions/{id}/events              SSE event stream
  GET    /api/health                                  liveness
  GET    /api/health/detail                           dependency status
  GET    /                                            mobile web client (static)
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from workbench_core.agent import AgentResult, ToolStep
from workbench_core.config import Settings, get_settings, reset_settings_cache
from workbench_core.llm import LLMClient

from leon_agent.agent import LeonAgent
from leon_agent.config import LeonSettings
from leon_agent.gateway.events import EventBusRegistry, LeonEvent
from leon_agent.image_modes import (
    DEFAULT_NSFW_MODE_ID,
    format_mode_catalog,
    mode_catalog_items,
    mode_display_name,
    parse_nsfw_command,
)
from leon_agent.leon_client import LeonImageClient
from leon_agent.models import model_provider_scope
from leon_agent.session import SessionStore
from leon_agent.tools import create_leon_tools

# ---------------------------------------------------------------------------
# Process-global singletons
# ---------------------------------------------------------------------------

_config: LeonSettings | None = None
_store: SessionStore | None = None
_bus_registry: EventBusRegistry = EventBusRegistry()
_llm_snapshots: dict[str, SessionLLMSnapshot] = {}

# Web static files — bundled inside the package at leon_agent/web/
_WEB_DIR: Path = Path(__file__).parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _store, _bus_registry, _llm_snapshots
    _config = LeonSettings()
    _store = SessionStore(_config.session_db)
    _bus_registry = EventBusRegistry()
    _llm_snapshots = {}
    try:
        yield
    finally:
        _config = None
        _store = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Leon Agent Gateway",
    version="0.1.0",
    description="HTTP + SSE gateway for the Leon Agent runtime.",
    lifespan=lifespan,
)


@app.middleware("http")
async def disable_web_shell_cache(request: Request, call_next):  # noqa: ANN001
    """Make installed PWAs pick up fixes instead of reusing a stale app shell."""
    response = await call_next(request)
    if request.url.path in {"/", "/index.html", "/sw.js"}:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    if request.url.path == "/sw.js":
        response.headers["Service-Worker-Allowed"] = "/"
    return response

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _request_has_valid_token(request: Request) -> bool:
    config = get_config()
    api_token = config.api_token.get_secret_value() if config.api_token else ""
    if not api_token:
        return True
    auth_header = request.headers.get("Authorization", "")
    query_token = request.query_params.get("token", "")
    return auth_header == f"Bearer {api_token}" or query_token == api_token


def verify_token(request: Request) -> None:
    """No-op when LEON_API_TOKEN is unset (local dev). Enforced in production."""
    if _request_has_valid_token(request):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_store() -> SessionStore:
    if _store is None:  # pragma: no cover
        raise RuntimeError("Store not initialised")
    return _store


def get_config() -> LeonSettings:
    if _config is None:  # pragma: no cover
        raise RuntimeError("Config not initialised")
    return _config


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class CreateSessionResponse(BaseModel):
    session_id: str
    created_at: int


class MessageRequest(BaseModel):
    content: str


class MessageResponse(BaseModel):
    session_id: str
    answer: str
    ok: bool


class ModelSelectionRequest(BaseModel):
    model: str | None = Field(default=None, max_length=200)


@dataclass
class SessionLLMSnapshot:
    settings: Settings
    models: list[str] | None = None
    catalog_error: str | None = None


def _capture_llm_snapshot() -> SessionLLMSnapshot:
    """Freeze the active TOML provider for one Web login/session."""
    reset_settings_cache()
    settings = get_settings()
    # Accessing profile loads and caches the complete TOML provider, including
    # its base URL, API key, and default model, inside this Settings instance.
    _ = (settings.profile, settings.active_base_url, settings.active_model)
    return SessionLLMSnapshot(settings=settings)


def _get_llm_snapshot(session_id: str) -> SessionLLMSnapshot:
    snapshot = _llm_snapshots.get(session_id)
    if snapshot is None:
        snapshot = _capture_llm_snapshot()
        _llm_snapshots[session_id] = snapshot
    return snapshot


def _create_image_client(config: LeonSettings) -> LeonImageClient:
    return LeonImageClient(
        backend_url=config.backend_url,
        plugin_dir=config.active_plugin_dir,
        public_base_url=config.active_public_image_base_url,
        timeout_seconds=config.http_timeout_seconds,
        bridge_timeout_seconds=config.bridge_timeout_seconds,
    )


async def _model_selection_response(
    store: SessionStore,
    session_id: str,
    *,
    refresh_catalog: bool = False,
) -> dict[str, Any]:
    snapshot = _get_llm_snapshot(session_id)
    settings = snapshot.settings
    selection = store.get_model_selection(session_id)
    scope = model_provider_scope(profile=settings.profile, base_url=settings.active_base_url)
    if selection and selection[0] != scope:
        store.set_model_selection(session_id, provider=None, model=None)
        selection = None
    selected_model = selection[1] if selection else None
    if refresh_catalog or snapshot.models is None:
        try:
            snapshot.models = await asyncio.to_thread(LLMClient(settings).list_models)
            snapshot.catalog_error = None
        except Exception as exc:  # noqa: BLE001 - custom model entry must remain usable
            snapshot.models = []
            snapshot.catalog_error = f"{type(exc).__name__}: {exc}"
    models = list(snapshot.models)
    for model_id in (settings.active_model, selected_model):
        if model_id and model_id not in models:
            models.append(model_id)
    return {
        "provider": settings.profile,
        "provider_scope": scope,
        "base_url": settings.active_base_url,
        "default_model": settings.active_model,
        "selected_model": selected_model,
        "active_model": selected_model or settings.active_model,
        "models": models,
        "catalog_error": snapshot.catalog_error,
    }


_TERMINAL_IMAGE_STATUSES = {"completed", "failed", "cancelled", "canceled"}


async def _track_image_jobs(
    *,
    bus: EventBusRegistry,
    session_id: str,
    store: SessionStore,
    image_client: LeonImageClient,
    chat_id: str,
    generation_plan_id: str | None,
    jobs: list[dict[str, Any]],
) -> None:
    """Publish task changes after the Agent returns its immediate submission response."""
    job_ids = {str(job["job_id"]) for job in jobs if job.get("job_id")}
    if not job_ids:
        return

    pending = set(job_ids)
    last_status: dict[str, tuple[str, Any]] = {}
    notified_jobs: set[str] = set()
    deadline = time.monotonic() + 300.0
    while pending and time.monotonic() < deadline:
        try:
            task_result = await asyncio.to_thread(
                image_client.get_image_tasks,
                chat_id=chat_id,
                limit=max(20, min(100, len(job_ids) * 4)),
            )
        except Exception:  # noqa: BLE001 - background status is best effort
            return

        task_items = task_result.get("items", []) if isinstance(task_result, dict) else []
        task_by_id = {
            str(item.get("job_id")): item
            for item in task_items
            if isinstance(item, dict) and str(item.get("job_id")) in pending
        }
        for job_id, item in task_by_id.items():
            status = str(item.get("status") or "queued").lower()
            progress = item.get("progress")
            marker = (status, progress)
            if last_status.get(job_id) != marker:
                last_status[job_id] = marker
                bus.get_or_create(session_id, asyncio.get_running_loop()).publish(
                    LeonEvent(
                        event="image.task.updated",
                        session_id=session_id,
                        data={"job_id": job_id, "status": status, "progress": progress},
                    )
                )
            if status in _TERMINAL_IMAGE_STATUSES and status != "completed":
                pending.remove(job_id)

        completed_ids = {
            job_id
            for job_id in pending
            if str(task_by_id.get(job_id, {}).get("status") or "").lower() == "completed"
        }
        if completed_ids:
            try:
                gallery_result = await asyncio.to_thread(
                    image_client.get_recent_images,
                    chat_id=chat_id,
                    limit=max(20, min(100, len(job_ids) * 4)),
                )
            except Exception:  # noqa: BLE001 - task remains visible even if gallery lags
                gallery_result = {"items": []}
            gallery_items = (
                gallery_result.get("items", []) if isinstance(gallery_result, dict) else []
            )
            completed_now: list[dict[str, str]] = []
            for item in gallery_items:
                if not isinstance(item, dict):
                    continue
                job_id = str(item.get("job_id") or "")
                image_url = item.get("image_url")
                if job_id not in completed_ids or not image_url or job_id in notified_jobs:
                    continue
                notified_jobs.add(job_id)
                bus.get_or_create(session_id, asyncio.get_running_loop()).publish(
                    LeonEvent(
                        event="image.completed",
                        session_id=session_id,
                        data={
                            "generation_plan_id": generation_plan_id,
                            "job_id": job_id,
                            "image_url": image_url,
                        },
                    )
                )
                pending.discard(job_id)
                completed_now.append({"job_id": job_id, "image_url": str(image_url)})
            if completed_now:
                count = len(completed_now)
                heading = "图片生成好了。" if count == 1 else f"{count} 张图片生成好了。"
                image_markdown = "\n\n".join(
                    f"![生成图片 {index}]({item['image_url']})"
                    for index, item in enumerate(completed_now, start=1)
                )
                content = f"{heading}\n\n{image_markdown}"
                completed_job_ids = [item["job_id"] for item in completed_now]
                store.add_message(session_id, "assistant", content)
                bus.get_or_create(session_id, asyncio.get_running_loop()).publish(
                    LeonEvent(
                        event="assistant.notice",
                        session_id=session_id,
                        data={"content": content, "job_ids": completed_job_ids},
                    )
                )
        if pending:
            await asyncio.sleep(2.0)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/api/health", tags=["health"], dependencies=[Depends(verify_token)])
async def health():
    """Authenticated liveness check, also used to validate the browser token."""
    return {"ok": True, "service": "leon-agent-gateway"}


@app.get("/api/health/detail", tags=["health"], dependencies=[Depends(verify_token)])
async def health_detail(config: LeonSettings = Depends(get_config)):
    results: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{config.backend_url}/system_stats")
            results["comfyui"] = "online" if r.status_code == 200 else "degraded"
    except Exception:
        results["comfyui"] = "offline"
    results["image_tool"] = "ready" if config.active_plugin_dir else "not_configured"
    results["llm"] = "unknown"
    return {"ok": True, "services": results}


@app.get("/api/image-modes", tags=["images"], dependencies=[Depends(verify_token)])
async def get_image_modes(config: LeonSettings = Depends(get_config)):
    image_client = _create_image_client(config)
    result = await asyncio.to_thread(image_client.list_modes)
    modes = result.get("modes", []) if isinstance(result, dict) else []
    return {
        "default_mode_id": DEFAULT_NSFW_MODE_ID,
        "default_mode_name": mode_display_name(DEFAULT_NSFW_MODE_ID),
        "modes": mode_catalog_items(modes),
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@app.post(
    "/api/agent/sessions",
    response_model=CreateSessionResponse,
    tags=["sessions"],
    dependencies=[Depends(verify_token)],
)
async def create_session(store: SessionStore = Depends(get_store)):
    session_id = store.create_session()
    _llm_snapshots[session_id] = _capture_llm_snapshot()
    return CreateSessionResponse(session_id=session_id, created_at=int(time.time() * 1000))


@app.get("/api/agent/sessions", tags=["sessions"], dependencies=[Depends(verify_token)])
async def list_sessions(store: SessionStore = Depends(get_store)):
    return {"sessions": store.list_sessions(limit=20)}


@app.get(
    "/api/agent/sessions/{session_id}",
    tags=["sessions"],
    dependencies=[Depends(verify_token)],
)
async def get_session(session_id: str, store: SessionStore = Depends(get_store)):
    if not store.has_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, "messages": store.load_messages(session_id, limit=100)}


@app.get(
    "/api/agent/sessions/{session_id}/messages",
    tags=["sessions"],
    dependencies=[Depends(verify_token)],
)
async def get_messages(session_id: str, store: SessionStore = Depends(get_store)):
    if not store.has_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"messages": store.load_messages(session_id, limit=100)}


@app.get(
    "/api/agent/sessions/{session_id}/image-state",
    tags=["images"],
    dependencies=[Depends(verify_token)],
)
async def get_session_image_state(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    store: SessionStore = Depends(get_store),
    config: LeonSettings = Depends(get_config),
):
    """Restore task and gallery state after a Web client refresh."""
    if not store.has_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    image_client = _create_image_client(config)
    chat_id = f"leon-agent:{session_id}"
    task_result, gallery_result = await asyncio.gather(
        asyncio.to_thread(image_client.get_image_tasks, chat_id=chat_id, limit=limit),
        asyncio.to_thread(image_client.get_recent_images, chat_id=chat_id, limit=limit),
        return_exceptions=True,
    )

    errors: dict[str, str] = {}
    if isinstance(task_result, BaseException):
        errors["tasks"] = f"{type(task_result).__name__}: {task_result}"
        tasks: list[dict[str, Any]] = []
    else:
        tasks = task_result.get("items", []) if isinstance(task_result, dict) else []

    if isinstance(gallery_result, BaseException):
        errors["gallery"] = f"{type(gallery_result).__name__}: {gallery_result}"
        gallery_items: list[dict[str, Any]] = []
    else:
        gallery_items = (
            gallery_result.get("items", []) if isinstance(gallery_result, dict) else []
        )

    images_by_job: dict[str, dict[str, Any]] = {
        str(item.get("job_id")): item
        for item in gallery_items
        if isinstance(item, dict) and item.get("job_id") and item.get("image_url")
    }
    for task in tasks:
        if not isinstance(task, dict):
            continue
        job_id = str(task.get("job_id") or "")
        image_url = task.get("image_url")
        if job_id and image_url and job_id not in images_by_job:
            images_by_job[job_id] = {
                "job_id": job_id,
                "workflow_name": task.get("workflow_name"),
                "source_text": task.get("source_text"),
                "image_url": image_url,
                "created_at": None,
            }

    return {
        "tasks": tasks,
        "images": list(images_by_job.values()),
        "errors": errors,
    }


@app.get(
    "/api/agent/sessions/{session_id}/model",
    tags=["sessions"],
    dependencies=[Depends(verify_token)],
)
async def get_session_model(
    session_id: str,
    refresh: bool = Query(default=False),
    store: SessionStore = Depends(get_store),
):
    if not store.has_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return await _model_selection_response(store, session_id, refresh_catalog=refresh)


@app.put(
    "/api/agent/sessions/{session_id}/model",
    tags=["sessions"],
    dependencies=[Depends(verify_token)],
)
async def set_session_model(
    session_id: str,
    body: ModelSelectionRequest,
    store: SessionStore = Depends(get_store),
):
    if not store.has_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    model = (body.model or "").strip()
    settings = _get_llm_snapshot(session_id).settings
    scope = model_provider_scope(profile=settings.profile, base_url=settings.active_base_url)
    store.set_model_selection(
        session_id,
        provider=scope if model else None,
        model=model or None,
    )
    return await _model_selection_response(store, session_id)


# ---------------------------------------------------------------------------
# Send message — triggers Agent loop
# ---------------------------------------------------------------------------


@app.post(
    "/api/agent/sessions/{session_id}/messages",
    response_model=MessageResponse,
    tags=["chat"],
    dependencies=[Depends(verify_token)],
)
async def send_message(
    session_id: str,
    body: MessageRequest,
    store: SessionStore = Depends(get_store),
    config: LeonSettings = Depends(get_config),
):
    if not store.has_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    loop = asyncio.get_running_loop()
    bus = _bus_registry.get_or_create(session_id, loop)

    bus.publish(
        LeonEvent(event="user.message", session_id=session_id, data={"content": body.content})
    )

    history = store.load_messages(session_id)
    store.add_message(session_id, "user", body.content)
    stripped_content = body.content.strip()
    folded_content = stripped_content.casefold()
    is_nsfw_command = folded_content == "/nsfw" or folded_content.startswith("/nsfw ")

    def on_event(event) -> None:  # noqa: ANN001
        kind = event.kind
        if kind == "tool_started":
            bus.publish(
                LeonEvent(
                    event="tool.started",
                    session_id=session_id,
                    data={"tool_name": event.tool_name, "input": event.arguments or {}},
                )
            )
        elif kind == "tool_finished" and event.result is not None:
            result: dict[str, Any] = event.result
            ok = bool(result.get("ok"))
            bus.publish(
                LeonEvent(
                    event="tool.finished",
                    session_id=session_id,
                    data={
                        "tool_name": event.tool_name,
                        "ok": ok,
                        "output": result,
                    },
                )
            )

    bus.publish(LeonEvent(event="assistant.started", session_id=session_id, data={}))

    try:
        image_client = _create_image_client(config)

        def on_generation_submitted(submission: dict[str, Any]) -> None:
            jobs = [
                job
                for job in submission.get("jobs", [])
                if isinstance(job, dict) and job.get("job_id")
            ]
            for job in jobs:
                bus.publish(
                    LeonEvent(
                        event="image.task.created",
                        session_id=session_id,
                        data={
                            "generation_plan_id": submission.get("generation_plan_id"),
                            "job_id": job["job_id"],
                            "status": job.get("status", "queued"),
                        },
                    )
                )
            if jobs:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        _track_image_jobs(
                            bus=_bus_registry,
                            session_id=session_id,
                            store=store,
                            image_client=image_client,
                            chat_id=f"leon-agent:{session_id}",
                            generation_plan_id=submission.get("generation_plan_id"),
                            jobs=jobs,
                        )
                    )
                )

        if is_nsfw_command:
            mode_result = await asyncio.to_thread(image_client.list_modes)
            modes = mode_result.get("modes", []) if isinstance(mode_result, dict) else []
            try:
                command = parse_nsfw_command(stripped_content, modes)
            except ValueError as exc:
                answer = f"{exc}\n\n{format_mode_catalog(modes)}"
                store.add_message(session_id, "assistant", answer)
                bus.publish(
                    LeonEvent(
                        event="assistant.completed",
                        session_id=session_id,
                        data={"content": answer},
                    )
                )
                return MessageResponse(session_id=session_id, answer=answer, ok=False)
            if command is None:
                answer = format_mode_catalog(modes)
                store.add_message(session_id, "assistant", answer)
                bus.publish(
                    LeonEvent(
                        event="assistant.completed",
                        session_id=session_id,
                        data={"content": answer},
                    )
                )
                return MessageResponse(session_id=session_id, answer=answer, ok=True)
            arguments = {
                "source_text": command.source_text,
                "workflow_ids": [command.workflow_id],
                "batch_count": 1,
            }
            bus.publish(
                LeonEvent(
                    event="tool.started",
                    session_id=session_id,
                    data={"tool_name": "generate_images", "input": arguments},
                )
            )
            direct_tools = create_leon_tools(
                image_client,
                session_id=session_id,
                default_mode_ids=config.default_mode_ids,
                wait_for_image_completion=False,
                on_generation_submitted=on_generation_submitted,
            )
            submission = await asyncio.to_thread(
                direct_tools.execute,
                "generate_images",
                arguments,
            )
            ok = bool(submission.get("ok"))
            bus.publish(
                LeonEvent(
                    event="tool.finished",
                    session_id=session_id,
                    data={
                        "tool_name": "generate_images",
                        "ok": ok,
                        "output": submission,
                    },
                )
            )
            answer = (
                f"已使用 {command.mode_name} 模式提交生图任务，完成后会自动在聊天里显示图片。"
                if ok
                else f"直达生图提交失败：{submission.get('error') or '未知错误'}"
            )
            result = AgentResult(
                answer=answer,
                steps=[ToolStep("generate_images", arguments, submission)],
            )
            store.record_result(session_id, result)
            store.add_message(session_id, "assistant", answer)
            bus.publish(
                LeonEvent(
                    event="assistant.completed",
                    session_id=session_id,
                    data={"content": answer},
                )
            )
            return MessageResponse(session_id=session_id, answer=answer, ok=ok)

        llm_settings = _get_llm_snapshot(session_id).settings
        model_selection = store.get_model_selection(session_id)
        scope = model_provider_scope(
            profile=llm_settings.profile,
            base_url=llm_settings.active_base_url,
        )
        if model_selection and model_selection[0] != scope:
            store.set_model_selection(session_id, provider=None, model=None)
            model_selection = None
        agent = LeonAgent(
            llm_client=LLMClient(
                llm_settings,
                model_override=model_selection[1] if model_selection else None,
            ),
            image_client=image_client,
            session_id=session_id,
            default_mode_ids=config.default_mode_ids,
            on_event=on_event,
            wait_for_image_completion=False,
            on_generation_submitted=on_generation_submitted,
        )

        result = await asyncio.to_thread(agent.run, body.content, history=history)

        store.record_result(session_id, result)
        store.add_message(session_id, "assistant", result.answer)

        bus.publish(
            LeonEvent(
                event="assistant.completed",
                session_id=session_id,
                data={"content": result.answer},
            )
        )
        return MessageResponse(session_id=session_id, answer=result.answer, ok=True)

    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        bus.publish(
            LeonEvent(event="agent.error", session_id=session_id, data={"error": err_msg})
        )
        raise HTTPException(status_code=500, detail=err_msg) from exc


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------


@app.get(
    "/api/agent/sessions/{session_id}/events",
    tags=["events"],
)
async def session_events(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(get_store),
):
    # 204 tells EventSource to stop retrying a stale token instead of flooding logs with 401s.
    if not _request_has_valid_token(request):
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    if not store.has_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    loop = asyncio.get_running_loop()
    bus = _bus_registry.get_or_create(session_id, loop)
    queue = bus.subscribe()

    async def event_generator():
        yield LeonEvent(event="session.connected", session_id=session_id, data={}).to_sse()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event is None:
                    break
                yield event.to_sse()
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Static web client — mount LAST so /api/* routes take priority
# ---------------------------------------------------------------------------

if _WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
