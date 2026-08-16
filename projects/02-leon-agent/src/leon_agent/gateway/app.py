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
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

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
from leon_agent.voice_client import VoiceError, VolinkVoiceClient, prepare_speech_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process-global singletons
# ---------------------------------------------------------------------------

_config: LeonSettings | None = None
_store: SessionStore | None = None
_bus_registry: EventBusRegistry = EventBusRegistry()
_llm_snapshots: dict[str, SessionLLMSnapshot] = {}

# Web static files. The Vue build is opt-in until its page-by-page migration is complete.
_LEGACY_WEB_DIR: Path = Path(__file__).parent.parent / "web"
_VUE_WEB_DIST_DIR: Path = Path(__file__).resolve().parents[3] / "web" / "dist"


def _resolve_web_dir(
    mode: str = "legacy",
    *,
    legacy_dir: Path = _LEGACY_WEB_DIR,
    vue_dist_dir: Path = _VUE_WEB_DIST_DIR,
) -> Path:
    selected = mode.strip().casefold()
    if selected == "legacy":
        return legacy_dir
    if selected == "vue":
        entrypoint = vue_dist_dir / "index.html"
        if not entrypoint.is_file():
            raise RuntimeError(
                "LEON_WEB_CLIENT=vue requires a built Vue client at "
                f"{vue_dist_dir}; run `npm install` and `npm run build` first"
            )
        return vue_dist_dir
    raise ValueError("LEON_WEB_CLIENT must be 'legacy' or 'vue'")


_WEB_DIR: Path = _resolve_web_dir(LeonSettings().web_client)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _store, _bus_registry, _llm_snapshots
    _config = LeonSettings()
    _config.read_additional_system_prompt()
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


def _fallback_image_completion(count: int) -> str:
    if count == 1:
        return "图已经生成好了，点开看看。"
    return f"{count} 张图已经生成好了，点开看看。"


def _image_sort_time(item: dict[str, Any]) -> int:
    try:
        return int(item.get("created_at") or 0)
    except (TypeError, ValueError):
        return 0


def _llm_image_completion_message(llm_client: LLMClient, count: int) -> str:
    """Ask the pinned session model for a short, human completion note."""
    fallback = _fallback_image_completion(count)
    prompt = (
        "图片生成任务刚刚完成。请用一句自然、简短、有人味的中文告诉用户图片已经好了，"
        "邀请他直接点开查看。不要输出图片 URL、Markdown、JSON、工具名或解释。"
        f"本次共有 {count} 张图片。"
    )
    try:
        answer = llm_client.chat(
            [
                {"role": "system", "content": "你是 Leon，一个说话自然的中文助手。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
        ).strip()
    except Exception:  # noqa: BLE001 - image delivery must not depend on this optional note
        return fallback
    return answer[:200] or fallback


def _session_image_completion_factory(
    *,
    session_id: str,
    store: SessionStore,
) -> Callable[[int], str]:
    def complete(count: int) -> str:
        snapshot = _get_llm_snapshot(session_id)
        selection = store.get_model_selection(session_id)
        model_override = None
        if selection:
            scope = model_provider_scope(
                profile=snapshot.settings.profile,
                base_url=snapshot.settings.active_base_url,
            )
            if selection[0] == scope:
                model_override = selection[1]
        return _llm_image_completion_message(
            LLMClient(snapshot.settings, model_override=model_override), count
        )

    return complete


async def _track_image_jobs(
    *,
    bus: EventBusRegistry,
    session_id: str,
    store: SessionStore,
    image_client: LeonImageClient,
    chat_id: str,
    generation_plan_id: str | None,
    jobs: list[dict[str, Any]],
    completion_message_factory: Callable[[int], str] | None = None,
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
            completed_now = [
                {
                    "job_id": job_id,
                    "image_url": str(task_by_id[job_id].get("image_url")),
                }
                for job_id in completed_ids
                if task_by_id[job_id].get("image_url") and job_id not in notified_jobs
            ]
            missing_image_ids = completed_ids - {item["job_id"] for item in completed_now}
            if missing_image_ids:
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
                gallery_seen: set[str] = set()
                for item in gallery_items:
                    if not isinstance(item, dict):
                        continue
                    job_id = str(item.get("job_id") or "")
                    image_url = item.get("image_url")
                    if (
                        job_id not in missing_image_ids
                        or not image_url
                        or job_id in notified_jobs
                        or job_id in gallery_seen
                    ):
                        continue
                    gallery_seen.add(job_id)
                    completed_now.append({"job_id": job_id, "image_url": str(image_url)})
            for item in completed_now:
                job_id = item["job_id"]
                notified_jobs.add(job_id)
                bus.get_or_create(session_id, asyncio.get_running_loop()).publish(
                    LeonEvent(
                        event="image.completed",
                        session_id=session_id,
                        data={
                            "generation_plan_id": generation_plan_id,
                            "job_id": job_id,
                            "image_url": item["image_url"],
                        },
                    )
                )
                pending.discard(job_id)
            if completed_now:
                count = len(completed_now)
                image_markdown = "\n\n".join(
                    f"![生成图片 {index}]({item['image_url']})"
                    for index, item in enumerate(completed_now, start=1)
                )
                store.add_message(session_id, "assistant", image_markdown)
                if completion_message_factory is not None:
                    try:
                        completion = await asyncio.to_thread(completion_message_factory, count)
                    except Exception:  # noqa: BLE001 - never lose the completion notice
                        completion = _fallback_image_completion(count)
                    store.add_message(session_id, "assistant", completion)
                    bus.get_or_create(session_id, asyncio.get_running_loop()).publish(
                        LeonEvent(
                            event="assistant.notice",
                            session_id=session_id,
                            data={
                                "content": completion,
                                "job_ids": [item["job_id"] for item in completed_now],
                            },
                        )
                    )
        if pending:
            await asyncio.sleep(1.0)


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

    tasks = [item for item in tasks if isinstance(item, dict)]
    for item in tasks:
        mode_id = str(item.get("workflow_name") or "").strip()
        item["mode_id"] = mode_id
        item["mode_name"] = mode_display_name(mode_id) if mode_id else ""
    tasks.sort(key=_image_sort_time, reverse=True)

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
                "created_at": task.get("created_at"),
            }

    ordered_images = sorted(
        images_by_job.values(),
        key=_image_sort_time,
        reverse=True,
    )

    return {
        "tasks": tasks,
        "images": ordered_images,
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
            workflow_ids = [
                str(item).strip()
                for item in submission.get("workflow_ids", [])
                if str(item).strip()
            ]
            source_text = str(submission.get("source_text") or "").strip()
            jobs = [
                job
                for job in submission.get("jobs", [])
                if isinstance(job, dict) and job.get("job_id")
            ]
            for index, job in enumerate(jobs):
                fallback_mode = (
                    workflow_ids[min(index, len(workflow_ids) - 1)]
                    if workflow_ids
                    else ""
                )
                mode_id = str(job.get("workflow_name") or fallback_mode).strip()
                bus.publish(
                    LeonEvent(
                        event="image.task.created",
                        session_id=session_id,
                        data={
                            "generation_plan_id": submission.get("generation_plan_id"),
                            "job_id": job["job_id"],
                            "status": job.get("status", "queued"),
                            "mode_id": mode_id,
                            "mode_name": mode_display_name(mode_id) if mode_id else "",
                            "source_text": source_text,
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
                            completion_message_factory=_session_image_completion_factory(
                                session_id=session_id,
                                store=store,
                            ),
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
            speak_handler=_session_speak_factory(config, session_id, bus.publish),
            additional_system_prompt=config.read_additional_system_prompt(),
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
    # Native EventSource sends Last-Event-ID on reconnect.  The query aliases
    # keep parity with clients that cannot set headers (and with older clients).
    last_event_id = request.headers.get("Last-Event-ID")
    if last_event_id is None:
        last_event_id = (
            request.query_params.get("last_event_id")
            or request.query_params.get("lastEventId")
            or request.query_params.get("last-event-id")
        )
    queue = bus.subscribe(last_event_id)

    async def event_generator():
        # This is a connection marker, not a replayable session event.  Keep it
        # data-only so it does not advance Last-Event-ID ahead of replayed data.
        yield LeonEvent(
            event="session.connected", session_id=session_id, data={}, id=None
        ).to_sse()
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
# Voice (TTS)
# ---------------------------------------------------------------------------


class VoiceClipStore:
    """Hold rendered mp3 bytes in memory so the browser never sees the API key.

    Clips are small (~50KB) and short-lived: the newest `max_count` survive and
    anything older than `ttl_seconds` is dropped on access.
    """

    def __init__(self, *, max_count: int = 200, ttl_seconds: float = 3600.0) -> None:
        self.max_count = max_count
        self.ttl_seconds = ttl_seconds
        self._clips: OrderedDict[str, tuple[float, bytes]] = OrderedDict()

    def put(self, audio: bytes) -> str:
        clip_id = uuid4().hex
        self._clips[clip_id] = (time.monotonic(), audio)
        while len(self._clips) > self.max_count:
            self._clips.popitem(last=False)
        return clip_id

    def get(self, clip_id: str) -> bytes | None:
        entry = self._clips.get(clip_id)
        if entry is None:
            return None
        created_at, audio = entry
        if time.monotonic() - created_at > self.ttl_seconds:
            self._clips.pop(clip_id, None)
            return None
        return audio


_voice_clips = VoiceClipStore()
_voice_client_cache: dict[str, VolinkVoiceClient] = {}


def _get_voice_client(config: LeonSettings) -> VolinkVoiceClient:
    if not config.voice_enabled:
        raise HTTPException(status_code=503, detail="Voice is not configured (VOLINK_API_KEY)")
    key = config.volink_api_key.get_secret_value()
    cached = _voice_client_cache.get(key)
    if cached is None:
        cached = VolinkVoiceClient(api_key=key, base_url=config.volink_base_url)
        _voice_client_cache[key] = cached
    return cached


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice_id: str | None = Field(default=None, max_length=64)


@app.get("/api/voice/catalog", tags=["voice"], dependencies=[Depends(verify_token)])
async def get_voice_catalog(
    refresh: bool = Query(default=False),
    config: LeonSettings = Depends(get_config),
):
    if not config.voice_enabled:
        return {"enabled": False, "models": [], "voices": [], "default_voice_id": ""}
    client = _get_voice_client(config)
    try:
        catalog = await asyncio.to_thread(client.catalog, refresh=refresh)
    except VoiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "enabled": True,
        "default_voice_id": config.volink_default_voice_id,
        **catalog,
    }


@app.post("/api/agent/tts", tags=["voice"], dependencies=[Depends(verify_token)])
async def synthesize_speech(
    body: SpeakRequest,
    config: LeonSettings = Depends(get_config),
):
    client = _get_voice_client(config)
    voice_id = (body.voice_id or config.volink_default_voice_id).strip()
    try:
        audio = await asyncio.to_thread(
            client.synthesize, text=body.text, voice_id=voice_id
        )
    except VoiceError as exc:
        logger.warning(
            "Volink TTS failed: voice_id=%s input_chars=%d speakable_chars=%d error=%s",
            voice_id,
            len(body.text),
            len(prepare_speech_text(body.text)),
            exc,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/voice/clips/{clip_id}", tags=["voice"])
async def get_voice_clip(clip_id: str, request: Request):
    # Audio elements cannot send an Authorization header, so this route accepts
    # the same ?token= query the SSE stream already relies on.
    if not _request_has_valid_token(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    audio = _voice_clips.get(clip_id)
    if audio is None:
        raise HTTPException(status_code=404, detail="Clip expired or not found")
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store", "Accept-Ranges": "none"},
    )


def _session_speak_factory(
    config: LeonSettings,
    session_id: str,
    bus_publish: Callable[[LeonEvent], None],
) -> Callable[[str, str | None], dict[str, Any]] | None:
    """Build the speak_text handler, or None when voice is not configured."""
    if not config.voice_enabled:
        return None
    client = _get_voice_client(config)

    def speak(text: str, voice_id: str | None = None) -> dict[str, Any]:
        target = (voice_id or config.volink_default_voice_id).strip()
        audio = client.synthesize(text=text, voice_id=target)
        clip_id = _voice_clips.put(audio)
        voice = client.resolve_voice(target) or {}
        bus_publish(
            LeonEvent(
                event="voice.ready",
                session_id=session_id,
                data={
                    "clip_id": clip_id,
                    "url": f"/api/voice/clips/{clip_id}",
                    "text": text,
                    "voice_id": target,
                    "voice_name": voice.get("name") or "",
                    "bytes": len(audio),
                },
            )
        )
        # The audio itself already reached the client over SSE; the model only
        # needs to know it worked so it does not repeat the text.
        return {
            "ok": True,
            "spoken": True,
            "voice_name": voice.get("name") or "",
            "characters": len(text),
        }

    return speak


# ---------------------------------------------------------------------------
# Static web client — mount LAST so /api/* routes take priority
# ---------------------------------------------------------------------------

if _WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
