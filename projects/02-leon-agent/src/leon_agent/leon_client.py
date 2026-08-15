"""Adapter from Agent tools to the existing Leon image-generation chain."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urljoin

import httpx

ABSOLUTE_URL_PREFIXES = ("http://", "https://", "data:", "blob:")


class LeonImageError(RuntimeError):
    pass


class Bridge(Protocol):
    def run(self, action: str, **payload: Any) -> dict[str, Any]: ...


class LeonNodeBridge:
    """Execute the plugin's request builder without copying its assets."""

    def __init__(self, plugin_dir: Path, *, timeout_seconds: float = 20.0) -> None:
        self.plugin_dir = plugin_dir.resolve()
        self.timeout_seconds = timeout_seconds
        self.script = Path(__file__).resolve().parents[2] / "bridge" / "leon_bridge.cjs"

    def run(self, action: str, **payload: Any) -> dict[str, Any]:
        if shutil.which("node") is None:
            raise LeonImageError("Node.js is required to reuse the Leon plugin request builder")
        if not self.plugin_dir.is_dir():
            raise LeonImageError(f"Leon plugin directory not found: {self.plugin_dir}")
        if not self.script.is_file():
            raise LeonImageError(f"Leon bridge script not found: {self.script}")

        process = subprocess.run(
            ["node", str(self.script), str(self.plugin_dir)],
            input=json.dumps({"action": action, **payload}, ensure_ascii=False),
            text=True,
            capture_output=True,
            encoding="utf-8",
            timeout=self.timeout_seconds,
            check=False,
        )
        if process.returncode != 0:
            detail = process.stderr.strip() or "unknown bridge error"
            raise LeonImageError(detail)
        try:
            result = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise LeonImageError("Leon bridge returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise LeonImageError("Leon bridge returned a non-object result")
        return result


class LeonImageClient:
    def __init__(
        self,
        *,
        backend_url: str,
        plugin_dir: Path,
        public_base_url: str | None = None,
        timeout_seconds: float = 30.0,
        bridge_timeout_seconds: float = 20.0,
        http_client: httpx.Client | None = None,
        bridge: Bridge | None = None,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        # Image URLs shown to the user must be openable outside this process, so a public
        # base URL wins over the internal backend address when both are configured.
        self.public_base_url = (public_base_url or self.backend_url).rstrip("/")
        self._http = http_client or httpx.Client(timeout=timeout_seconds)
        self.bridge = bridge or LeonNodeBridge(
            plugin_dir,
            timeout_seconds=bridge_timeout_seconds,
        )

    def _url(self, path: str) -> str:
        return f"{self.backend_url}/{path.lstrip('/')}"

    def _absolute_media_url(self, value: Any) -> str | None:
        """Turn a backend media reference into an absolute, openable URL.

        The Leon/ComfyUI sync endpoints may return relative paths such as
        ``/view?filename=a.png``. Returning those unchanged leaves the model and CLI
        printing hostless paths, so resolve them against the public base URL.
        """
        if not isinstance(value, str):
            return None
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.startswith(ABSOLUTE_URL_PREFIXES):
            return candidate
        if candidate.startswith("//"):
            return urljoin(f"{self.public_base_url}/", candidate)
        return f"{self.public_base_url}/{candidate.lstrip('/')}"

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._http.request(
                method,
                self._url(path),
                json=payload,
                params=params,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("error")
            except (ValueError, AttributeError):
                detail = None
            raise LeonImageError(detail or f"HTTP {exc.response.status_code}: {path}") from exc
        except httpx.HTTPError as exc:
            raise LeonImageError(f"Leon backend unavailable: {exc}") from exc
        try:
            return response.json()
        except ValueError as exc:
            raise LeonImageError(f"Leon backend returned invalid JSON: {path}") from exc

    def _check_route(self, path: str) -> bool:
        try:
            response = self._http.get(self._url(path))
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LeonImageError(f"Leon route unavailable: {path}: {exc}") from exc
        return True

    def list_modes(self) -> dict[str, Any]:
        result = self.bridge.run("list_modes")
        return {"ok": True, "modes": result.get("modes", [])}

    def check_environment(self) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        try:
            system_stats = self._json_request("GET", "/system_stats")
            checks["backend"] = {"ok": isinstance(system_stats, dict)}
            checks["ios_dashboard"] = {"ok": self._check_route("/ios/dashboard")}
            object_info = self._json_request("GET", "/object_info")
            checks["object_info"] = {"ok": isinstance(object_info, dict)}
            loras = self._json_request("GET", "/models/loras")
            checks["loras"] = {"ok": isinstance(loras, list), "count": len(loras or [])}
            report = self.bridge.run(
                "inspect_environment",
                object_info=object_info,
                lora_names=loras if isinstance(loras, list) else [],
            )
            checks["assets"] = report
        except LeonImageError as exc:
            return {
                "ok": False,
                "backend_url": self.backend_url,
                "public_base_url": self.public_base_url,
                "checks": checks,
                "error": str(exc),
            }
        return {
            "ok": all(bool(item.get("ok")) for item in checks.values()),
            "backend_url": self.backend_url,
            "public_base_url": self.public_base_url,
            "checks": checks,
        }

    def generate_images(
        self,
        *,
        source_text: str,
        workflow_ids: list[str],
        batch_count: int,
        chat_id: str,
        message_id: str,
        character_context: str = "",
        random_workflow: bool = False,
    ) -> dict[str, Any]:
        request = self.bridge.run(
            "build_request",
            options={
                "sourceText": source_text,
                "workflowIds": workflow_ids,
                "batchCount": batch_count,
                "chatId": chat_id,
                "messageId": message_id,
                "characterContext": character_context,
                "randomWorkflow": random_workflow,
            },
        )
        body = request.get("body")
        if not isinstance(body, dict):
            raise LeonImageError("Leon bridge did not produce a request body")
        body["source_from"] = "leon_agent_cli"
        response = self._json_request("POST", "/ios/async_autogen", payload=body)
        if not isinstance(response, dict):
            raise LeonImageError("Leon backend returned a non-object generation response")
        jobs = []
        for item in response.get("jobs", []):
            if not isinstance(item, dict):
                continue
            jobs.append(
                {
                    "job_id": item.get("job_id"),
                    "client_task_id": item.get("client_task_id"),
                    "status": item.get("status", "queued"),
                    "workflow_name": item.get("workflow_name"),
                }
            )
        return {
            "ok": True,
            "generation_plan_id": body.get("generation_plan_id"),
            "output_count": request.get("outputCount", len(jobs)),
            "jobs": jobs,
        }

    def cancel_image_task(self, *, job_id: str) -> dict[str, Any]:
        """Cancel one queued or running job and interrupt its ComfyUI prompt.

        The backend treats an already finished job as a no-op and answers with the
        terminal status, so the caller can report the truth instead of an error.
        """
        cleaned = str(job_id or "").strip()
        if not cleaned:
            raise LeonImageError("job_id is required to cancel an image task")
        response = self._json_request(
            "POST",
            f"/ios/async_autogen/{quote(cleaned, safe='')}/cancel",
        )
        if not isinstance(response, dict):
            raise LeonImageError("Leon backend returned a non-object cancel response")
        status = str(response.get("status") or "cancelled")
        return {
            "ok": True,
            "job_id": str(response.get("job_id") or cleaned),
            "status": status,
            "cancelled": status == "cancelled",
        }

    def get_image_tasks(self, *, chat_id: str, limit: int = 20) -> dict[str, Any]:
        response = self._sync("/ios/image_tasks/sync", chat_id=chat_id, limit=limit)
        return {"ok": True, "items": [self._task_summary(item) for item in response]}

    def get_recent_images(self, *, chat_id: str, limit: int = 20) -> dict[str, Any]:
        response = self._sync("/ios/image_gallery/sync", chat_id=chat_id, limit=limit)
        return {"ok": True, "items": [self._image_summary(item) for item in response]}

    def get_latest_images(self, *, limit: int) -> dict[str, Any]:
        """Return the requested number of newest images across the Leon database."""
        safe_limit = max(1, min(int(limit), 100))
        response = self._json_request(
            "GET",
            "/ios/image_gallery/recent",
            params={"limit": safe_limit},
        )
        items = response.get("items", []) if isinstance(response, dict) else []
        return {
            "ok": True,
            "items": [self._image_summary(item) for item in items if isinstance(item, dict)][
                :safe_limit
            ],
        }

    def get_latest_image(self) -> dict[str, Any]:
        """Compatibility helper for callers that still need exactly one image."""
        response = self._json_request("GET", "/ios/image_gallery/latest")
        item = response.get("item") if isinstance(response, dict) else None
        return {
            "ok": True,
            "item": self._image_summary(item) if isinstance(item, dict) else None,
        }

    def _sync(self, path: str, *, chat_id: str, limit: int) -> list[dict[str, Any]]:
        payload = {
            "chat_id": chat_id,
            "after_event_id": 0,
            "legacy_message_ids": [],
            "pending_job_ids": [],
            "limit": max(1, min(limit, 100)),
        }
        result = self._json_request("POST", path, payload=payload)
        if not isinstance(result, dict):
            raise LeonImageError(f"Leon backend returned a non-object sync response: {path}")
        items = result.get("items", [])
        return [item for item in items if isinstance(item, dict)][: payload["limit"]]

    def _task_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": item.get("jobId") or item.get("job_id"),
            "status": item.get("status"),
            "progress": item.get("progress") or item.get("progress_pct"),
            "workflow_name": item.get("workflowName") or item.get("workflow_name"),
            "source_text": item.get("sourceText") or item.get("source_text"),
            "image_url": self._absolute_media_url(
                item.get("imageUrl")
                or item.get("image_url")
                or item.get("finalImageUrl")
                or item.get("final_image_url")
            ),
            "created_at": (
                item.get("createdAt")
                or item.get("created_at")
                or item.get("queuedAt")
                or item.get("queued_at")
                or item.get("queued_at_ms")
            ),
            "error": item.get("error"),
        }

    def _image_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": item.get("jobId") or item.get("job_id"),
            "workflow_name": item.get("workflowName") or item.get("workflow_name"),
            "source_text": item.get("sourceText") or item.get("source_text"),
            "image_url": self._absolute_media_url(
                item.get("imageUrl")
                or item.get("image_url")
                or item.get("finalImageUrl")
                or item.get("final_image_url")
            ),
            "created_at": (
                item.get("createdAt")
                or item.get("created_at")
                or item.get("finalizedAt")
                or item.get("finalized_at_ms")
                or item.get("finishedAt")
                or item.get("finished_at_ms")
                or item.get("queuedAt")
                or item.get("queued_at_ms")
            ),
        }
