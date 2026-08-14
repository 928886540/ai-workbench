"""Leon Agent HTTP Gateway — FastAPI application.

Phase 1 scope:
  POST   /api/agent/sessions                          create session
  GET    /api/agent/sessions                          list sessions
  GET    /api/agent/sessions/{id}                     session detail + history
  GET    /api/agent/sessions/{id}/messages            message history
  POST   /api/agent/sessions/{id}/messages            send message (runs Agent)
  GET    /api/agent/sessions/{id}/events              SSE event stream
  GET    /api/health                                  liveness
  GET    /api/health/detail                           dependency status
  GET    /                                            mobile web client (static)
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from workbench_core.config import get_settings, reset_settings_cache
from workbench_core.llm import LLMClient

from leon_agent.agent import LeonAgent
from leon_agent.config import LeonSettings
from leon_agent.gateway.events import EventBusRegistry, LeonEvent
from leon_agent.leon_client import LeonImageClient
from leon_agent.session import SessionStore

# ---------------------------------------------------------------------------
# Process-global singletons
# ---------------------------------------------------------------------------

_config: LeonSettings | None = None
_store: SessionStore | None = None
_bus_registry: EventBusRegistry = EventBusRegistry()

# Web static files — bundled inside the package at leon_agent/web/
_WEB_DIR: Path = Path(__file__).parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _store
    _config = LeonSettings()
    _store = SessionStore(_config.session_db)
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Leon Agent Gateway",
    version="0.1.0",
    description="HTTP + SSE gateway for the Leon Agent runtime.",
    lifespan=lifespan,
)

_cors_origins = [o.strip() for o in os.environ.get("LEON_CORS_ORIGINS", "*").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_API_TOKEN = os.environ.get("LEON_API_TOKEN", "")


def verify_token(request: Request) -> None:
    """No-op when LEON_API_TOKEN is unset (local dev). Enforced in production."""
    if not _API_TOKEN:
        return
    auth_header = request.headers.get("Authorization", "")
    query_token = request.query_params.get("token", "")
    if auth_header == f"Bearer {_API_TOKEN}" or query_token == _API_TOKEN:
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


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/api/health", tags=["health"])
async def health():
    """Public liveness check — no auth required (used by login screen to verify token)."""
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
    request: Request,
    store: SessionStore = Depends(get_store),
    config: LeonSettings = Depends(get_config),
):
    if not store.has_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    loop = asyncio.get_event_loop()
    bus = _bus_registry.get_or_create(session_id, loop)

    bus.publish(
        LeonEvent(event="user.message", session_id=session_id, data={"content": body.content})
    )

    history = store.load_messages(session_id)
    store.add_message(session_id, "user", body.content)

    def on_event(event) -> None:  # noqa: ANN001
        kind = event.kind
        if kind == "tool_started":
            bus.publish(
                LeonEvent(
                    event="tool.started",
                    session_id=session_id,
                    data={"tool_name": event.tool_name},
                )
            )
        elif kind == "tool_finished" and event.result is not None:
            result: dict[str, Any] = event.result
            ok = bool(result.get("ok"))
            bus.publish(
                LeonEvent(
                    event="tool.finished",
                    session_id=session_id,
                    data={"tool_name": event.tool_name, "ok": ok},
                )
            )
            plan_id = result.get("generation_plan_id")
            for job in result.get("jobs", []):
                if isinstance(job, dict) and job.get("job_id"):
                    bus.publish(
                        LeonEvent(
                            event="image.task.created",
                            session_id=session_id,
                            data={
                                "generation_plan_id": plan_id,
                                "job_id": job["job_id"],
                                "status": job.get("status", "queued"),
                            },
                        )
                    )
            for img in result.get("images", []):
                if isinstance(img, dict) and img.get("image_url"):
                    bus.publish(
                        LeonEvent(
                            event="image.completed",
                            session_id=session_id,
                            data={
                                "generation_plan_id": plan_id,
                                "job_id": img.get("job_id"),
                                "image_url": img["image_url"],
                            },
                        )
                    )

    bus.publish(LeonEvent(event="assistant.started", session_id=session_id, data={}))

    try:
        reset_settings_cache()
        llm_client = LLMClient(get_settings())
        image_client = LeonImageClient(
            backend_url=config.backend_url,
            plugin_dir=config.active_plugin_dir,
            public_base_url=config.active_public_image_base_url,
            timeout_seconds=config.http_timeout_seconds,
            bridge_timeout_seconds=config.bridge_timeout_seconds,
        )
        agent = LeonAgent(
            llm_client=llm_client,
            image_client=image_client,
            session_id=session_id,
            default_mode_ids=config.default_mode_ids,
            on_event=on_event,
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
        bus.publish_done()

        return MessageResponse(session_id=session_id, answer=result.answer, ok=True)

    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        bus.publish(
            LeonEvent(event="agent.error", session_id=session_id, data={"error": err_msg})
        )
        bus.publish_done()
        raise HTTPException(status_code=500, detail=err_msg) from exc


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------


@app.get(
    "/api/agent/sessions/{session_id}/events",
    tags=["events"],
    dependencies=[Depends(verify_token)],
)
async def session_events(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(get_store),
):
    if not store.has_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    loop = asyncio.get_event_loop()
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
