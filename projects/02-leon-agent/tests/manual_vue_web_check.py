"""Provider-free browser smoke check for the opt-in Vue Web client.

The script deliberately uses Playwright request interception instead of a real
LLM, image backend, or Volink.  By default it starts ``vite preview`` against
the already-built ``web/dist`` directory.  Pass ``--base-url`` (or
``LEON_VUE_BASE_URL``) to reuse a running FastAPI instance configured with
FastAPI's canonical Vue static entry.  The only runtime dependency is Playwright itself:

    uv run --with playwright python projects/02-leon-agent/tests/manual_vue_web_check.py

If the ephemeral environment cannot find a bundled browser, set ``CHROME_PATH``
or pass ``--chrome-path`` to use the installed Chrome.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = ROOT / "projects" / "02-leon-agent"
WEB_ROOT = PROJECT_ROOT / "web"
DEFAULT_PORT = 4173
FAKE_SESSION_ID = "vue-e2e-session"
FAKE_TOKEN = "vue-e2e-token"
FAKE_VOICE_ID = "689334e84d3396ad1d28ee9e"
JOK_VOICE_ID = "jok-voice-unused"


FAKE_BROWSER_SCRIPT = r"""
(() => {
  // Only scrub storage on first boot; reloads keep token/session so the
  // restore path (GET /sessions/:id with created_at) can be exercised.
  if (!sessionStorage.getItem("leon-smoke-booted")) {
    sessionStorage.setItem("leon-smoke-booted", "1");
    localStorage.removeItem("leon_token");
    localStorage.removeItem("leon_session");
    localStorage.removeItem("leon_voice_prefs");
  }

  // EventSource is intentionally deterministic. Tests inject protocol events
  // and connection failures without opening a real provider connection.
  const instances = [];
  class FakeEventSource {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 2;
    constructor(url) {
      this.url = String(url);
      this.readyState = FakeEventSource.OPEN;
      this.onmessage = null;
      this.onerror = null;
      instances.push(this);
      queueMicrotask(() => {
        if (this.readyState !== FakeEventSource.OPEN) return;
        if (typeof this.onmessage === "function") {
          this.onmessage({
            data: JSON.stringify({
              event: "session.connected",
              session_id: "vue-e2e-session",
              data: {},
            }),
          });
        }
      });
    }
    close() {
      this.readyState = FakeEventSource.CLOSED;
    }
  }
  Object.defineProperty(window, "EventSource", {
    configurable: true,
    writable: true,
    value: FakeEventSource,
  });
  window.__leonEmit = (event, data = {}) => {
    const payload = JSON.stringify({
      event,
      session_id: "vue-e2e-session",
      timestamp: new Date().toISOString(),
      data,
    });
    instances
      .filter((source) => source.readyState === 1)
      .forEach((source) => source.onmessage?.({ data: payload }));
  };
  window.__leonFailEvents = (permanent = false) => {
    instances
      .filter((source) => source.readyState !== FakeEventSource.CLOSED)
      .forEach((source) => {
        source.readyState = permanent ? FakeEventSource.CLOSED : FakeEventSource.CONNECTING;
        source.onerror?.(new Event("error"));
      });
  };

  const nativeFetch = window.fetch.bind(window);
  window.__leonCompleteRetryBeforePost = false;
  window.fetch = async (...args) => {
    const response = await nativeFetch(...args);
    const request = args[0];
    const init = args[1] || {};
    const url = typeof request === "string" ? request : request?.url || "";
    if (
      window.__leonCompleteRetryBeforePost &&
      String(url).includes("/api/agent/sessions/") &&
      String(url).endsWith("/messages") &&
      String(init.method || "GET").toUpperCase() === "POST"
    ) {
      let body = {};
      try { body = JSON.parse(String(init.body || "{}")); } catch {}
      if (body.retry) {
        window.__leonCompleteRetryBeforePost = false;
        window.__leonEmit("assistant.completed", {
          content: "这是重试后的本地回复。",
        });
      }
    }
    return response;
  };

  // No actual media device is needed for this provider-free check.  Keeping
  // the promise resolved lets the voice.ready path exercise the singleton
  // player without invoking browser autoplay policy.
  HTMLMediaElement.prototype.play = () => Promise.resolve();
  HTMLMediaElement.prototype.pause = () => {};
})();
"""


def _json_body(request: Any) -> dict[str, Any]:
    # Multipart audio uploads are binary; post_data would raise on decode.
    buffer = request.post_data_buffer
    raw = buffer.decode("utf-8", errors="ignore") if buffer else "{}"
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_response(route: Any, payload: dict[str, Any], status: int = 200) -> None:
    route.fulfill(
        status=status,
        content_type="application/json; charset=utf-8",
        body=json.dumps(payload, ensure_ascii=False),
    )


class FakeGateway:
    """Small HTTP fixture matching only the Vue client's API boundary."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.token_valid = True
        self.active_turn: dict[str, bool] | None = None
        self.cancel_post_405 = False
        self._next_message_id = 3
        self.session_messages: list[dict[str, Any]] = [
            {"id": 1, "role": "user", "content": "历史消息一", "created_at": 1_000},
            {
                "id": 2,
                "role": "assistant",
                "content": "历史回复一",
                "created_at": 1_000_000,
                "revisions": [],
            },
        ]

    def append_session_message(self, role: str, content: str) -> dict[str, Any]:
        message: dict[str, Any] = {
            "id": self._next_message_id,
            "role": role,
            "content": content,
            "created_at": 1_000_000 + self._next_message_id * 1_000,
        }
        self._next_message_id += 1
        if role == "assistant":
            message["revisions"] = []
        self.session_messages.append(message)
        return message

    def handle(self, route: Any) -> None:
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path
        method = request.method.upper()
        body = _json_body(request) if method in {"POST", "PUT", "PATCH"} else {}
        self.calls.append({"method": method, "path": path, "body": body})

        if path == "/api/health" and method == "GET":
            authorized = (
                self.token_valid
                and request.headers.get("authorization") == f"Bearer {FAKE_TOKEN}"
            )
            if not authorized:
                _json_response(route, {"detail": "Unauthorized"}, status=401)
            else:
                _json_response(route, {"ok": True, "service": "fake-vue-gateway"})
            return

        if path == "/api/agent/sessions" and method == "POST":
            _json_response(route, {"session_id": FAKE_SESSION_ID, "created_at": 1})
            return

        session_prefix = f"/api/agent/sessions/{FAKE_SESSION_ID}"
        if path == session_prefix and method == "GET":
            _json_response(
                route,
                {
                    "session_id": FAKE_SESSION_ID,
                    "messages": self.session_messages,
                    "active_turn": self.active_turn,
                },
            )
            return
        if path == "/api/agent/asr/status" and method == "GET":
            _json_response(route, {"enabled": True})
            return
        if path == "/api/agent/asr" and method == "POST":
            _json_response(route, {"text": "这是语音识别出来的文字"})
            return
        if path == f"{session_prefix}/image-state" and method == "GET":
            _json_response(
                route,
                {
                    "tasks": [
                        {
                            "job_id": "fake-image-job",
                            "status": "completed",
                            "progress": 100,
                            "mode_name": "蒂法增强",
                            "source_text": "测试图片",
                            "image_url": "/api/fake-image",
                            "created_at": 1,
                        },
                        {
                            "job_id": "queued-job",
                            "status": "queued",
                            "progress": 0,
                            "mode_name": "写实基础",
                            "source_text": "再来一张",
                            "created_at": 2,
                        },
                    ],
                    "images": [
                        {
                            "job_id": "fake-image-job",
                            "image_url": "/api/fake-image",
                            "source_text": "测试图片",
                            "created_at": 1,
                        },
                        {
                            "job_id": "fake-image-job-2",
                            "image_url": "/api/fake-image",
                            "source_text": "第二张测试图片",
                            "created_at": 2,
                        },
                    ],
                    "errors": {},
                },
            )
            return
        if path in {"/api/fake-image", "/api/fake-image-2"} and method == "GET":
            is_portrait = "latest-" in parsed.query
            width, height = (1536, 2500) if is_portrait else (4, 3)
            fake_svg = (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
                '<rect width="100%" height="100%" fill="#2783de"/></svg>'
            ).encode()
            route.fulfill(status=200, content_type="image/svg+xml", body=fake_svg)
            return
        if path == f"{session_prefix}/messages" and method == "POST":
            answer = (
                "这是重试后的本地回复。"
                if body.get("retry")
                else "这是 provider-free 的本地回复。"
            )
            content = str(body.get("content") or "")
            if (
                body.get("retry")
                and len(self.session_messages) >= 2
                and self.session_messages[-2].get("role") == "user"
                and self.session_messages[-1].get("role") == "assistant"
            ):
                self.session_messages[-2]["content"] = content
                assistant = self.session_messages[-1]
                revisions = assistant.setdefault("revisions", [])
                revisions.append(
                    {
                        "content": assistant.get("content", ""),
                        "created_at": assistant.get("created_at", 0),
                    }
                )
                assistant["content"] = answer
                assistant["created_at"] = int(assistant.get("created_at", 0)) + 1
            else:
                self.append_session_message("user", content)
                self.append_session_message("assistant", answer)
            _json_response(
                route,
                {
                    "session_id": FAKE_SESSION_ID,
                    "answer": answer,
                    "ok": True,
                },
            )
            return
        if path == f"{session_prefix}/cancel" and method == "POST":
            if self.cancel_post_405:
                _json_response(route, {"detail": "Method Not Allowed"}, status=405)
                return
            self.active_turn = None
            _json_response(
                route,
                {"session_id": FAKE_SESSION_ID, "cancelled": True},
            )
            return
        if path == f"{session_prefix}/cancel" and method == "DELETE":
            self.active_turn = None
            _json_response(
                route,
                {"session_id": FAKE_SESSION_ID, "cancelled": True},
            )
            return
        if path == f"{session_prefix}/model" and method == "GET":
            _json_response(route, self._model_payload())
            return
        if path == f"{session_prefix}/model" and method == "PUT":
            payload = self._model_payload()
            selected = body.get("model")
            payload["selected_model"] = selected if isinstance(selected, str) else None
            payload["active_model"] = selected or payload["default_model"]
            _json_response(route, payload)
            return

        if path == "/api/image-modes" and method == "GET":
            _json_response(
                route,
                {
                    "default_mode_id": "k2_tifa_plus",
                    "default_mode_name": "蒂法增强",
                    "modes": [
                        {
                            "id": "k2_tifa_plus",
                            "name": "蒂法增强",
                            "aliases": ["tifa-plus"],
                        },
                        {
                            "id": "k2_queen_marika",
                            "name": "玛莉卡",
                            "aliases": ["marika"],
                        },
                    ],
                },
            )
            return

        if path == "/api/voice/catalog" and method == "GET":
            _json_response(
                route,
                {
                    "enabled": True,
                    "default_voice_id": JOK_VOICE_ID,
                    "models": [{"id": "index-tts2", "name": "Fake TTS"}],
                    "voices": [
                        {
                            "id": JOK_VOICE_ID,
                            "name": "JOK",
                            "model": "index-tts2",
                            "languages": ["zh-CN"],
                            "demo": None,
                        },
                        {
                            "id": FAKE_VOICE_ID,
                            "name": "测试音色",
                            "model": "index-tts2",
                            "languages": ["zh-CN"],
                            "demo": "/api/voice/clips/demo-voice",
                        }
                    ]
                    + [
                        {
                            "id": f"fake-voice-{index:02d}",
                            "name": f"扩展音色 {index:02d}",
                            "model": "index-tts2",
                            "languages": ["zh-CN"],
                            "demo": None,
                        }
                        for index in range(1, 46)
                    ],
                },
            )
            return

        if path == "/api/agent/tts" and method == "POST":
            route.fulfill(status=200, content_type="audio/mpeg", body=b"fake-mp3")
            return

        if path.startswith("/api/voice/clips/") and method == "GET":
            route.fulfill(status=200, content_type="audio/mpeg", body=b"fake-mp3")
            return

        # The browser EventSource is replaced by the fixture, but returning a
        # valid one-shot response makes accidental network use diagnosable.
        if path.startswith(f"{session_prefix}/events") and method == "GET":
            route.fulfill(
                status=200,
                content_type="text/event-stream",
                body=(
                    "data: "
                    + json.dumps(
                        {
                            "event": "session.connected",
                            "session_id": FAKE_SESSION_ID,
                            "data": {},
                        }
                    )
                    + "\n\n"
                ),
            )
            return

        _json_response(route, {"detail": f"Unhandled fake API route: {method} {path}"}, 404)

    @staticmethod
    def _model_payload() -> dict[str, Any]:
        return {
            "provider": "fake",
            "provider_scope": "fake",
            "base_url": "http://fake.invalid/v1",
            "default_model": "fake-default",
            "selected_model": None,
            "active_model": "fake-default",
            "models": ["fake-default", "Fake-Case-Sensitive"],
            "catalog_error": None,
        }


@dataclass
class ServerHandle:
    base_url: str
    process: subprocess.Popen[str] | None = None
    temporary_dir: tempfile.TemporaryDirectory[str] | None = None

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.temporary_dir is not None:
            self.temporary_dir.cleanup()


def _command(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt" and name == "npm":
        return "npm.cmd"
    return name


def _wait_for_server(base_url: str, process: subprocess.Popen[str] | None) -> None:
    deadline = time.monotonic() + 20
    last_error = ""
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"本地服务提前退出，exit={process.returncode}")
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=1) as response:
                if response.status < 500:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"等待本地 Vue 服务超时：{base_url} ({last_error})")


def _start_server(args: argparse.Namespace) -> ServerHandle:
    configured = (args.base_url or os.environ.get("LEON_VUE_BASE_URL") or "").rstrip("/")
    if configured:
        return ServerHandle(configured)

    dist_entry = WEB_ROOT / "dist" / "index.html"
    if not dist_entry.is_file():
        npm = _command("npm")
        subprocess.run([npm, "--prefix", str(WEB_ROOT), "run", "build"], cwd=ROOT, check=True)

    port = args.port
    base_url = f"http://127.0.0.1:{port}"
    temporary_dir: tempfile.TemporaryDirectory[str] | None = None
    env = os.environ.copy()
    if args.server == "fastapi":
        temporary_dir = tempfile.TemporaryDirectory(prefix="leon-vue-e2e-")
        env["LEON_SESSION_DB"] = str(Path(temporary_dir.name) / "leon.db")
        command = [
            sys.executable,
            "-m",
            "leon_agent.gateway.server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
    else:
        command = [
            _command("npm"),
            "--prefix",
            str(WEB_ROOT),
            "run",
            "preview",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        text=True,
    )
    handle = ServerHandle(base_url, process, temporary_dir)
    try:
        _wait_for_server(base_url, process)
    except Exception:
        handle.close()
        raise
    return handle


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="",
        help="复用已启动的 Vue/FastAPI 地址；也可用 LEON_VUE_BASE_URL",
    )
    parser.add_argument(
        "--server",
        choices=("vite", "fastapi"),
        default="vite",
        help="未指定 --base-url 时启动的本地静态服务（默认 vite preview）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LEON_VUE_PORT", DEFAULT_PORT)),
    )
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--chrome-path", default=os.environ.get("CHROME_PATH", ""))
    parser.add_argument("--timeout", type=float, default=10_000, help="单步超时（毫秒）")
    parser.add_argument("--screenshot", default="", help="可选：保存最终页面截图路径")
    return parser


def run_browser_check(base_url: str, args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print(
            "缺少 Python Playwright。请使用："
            "uv run --with playwright python projects/02-leon-agent/tests/manual_vue_web_check.py",
            file=sys.stderr,
        )
        return 2

    gateway = FakeGateway()
    checks: list[tuple[str, bool, str]] = []
    page_errors: list[str] = []
    console_errors: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        checks.append((name, bool(condition), detail))
        print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))

    chrome_path = args.chrome_path.strip()
    if not chrome_path:
        candidates = (
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        )
        chrome_path = next((str(path) for path in candidates if path.is_file()), "")

    with sync_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": not args.headed,
            "args": [
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
            ],
        }
        if chrome_path:
            launch_options["executable_path"] = chrome_path
        try:
            browser = playwright.chromium.launch(**launch_options)
        except PlaywrightError as exc:
            print(
                f"无法启动 Chromium：{exc}\n"
                "可设置 CHROME_PATH，或执行 `uv run --with playwright "
                "playwright install chromium`。",
                file=sys.stderr,
            )
            return 2

        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=1,
            has_touch=True,
            is_mobile=True,
            service_workers="block",
            permissions=["microphone"],
        )
        page = context.new_page()
        page.set_default_timeout(args.timeout)
        page.add_init_script(FAKE_BROWSER_SCRIPT)
        page.route("**/api/**", gateway.handle)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )

        try:
            page.goto(base_url, wait_until="domcontentloaded")
            login_heading = page.get_by_role("heading", name="Leon")
            login_heading.wait_for(state="visible")
            check("登录页可见（初始 token 被拒绝）", login_heading.is_visible())
            check(
                "登录页文案真实且不会插入临时加载行",
                page.locator("#token").get_attribute("placeholder") == "输入访问口令"
                and "回到对话" not in page.locator(".login-view").inner_text()
                and "正在验证连接" not in page.locator(".login-view").inner_text(),
            )

            page.locator("#token").fill(FAKE_TOKEN)
            page.get_by_role("button", name="连接").click()
            page.get_by_role("heading", name="Leon").wait_for(state="visible")
            page.get_by_text("已连接").wait_for(state="visible")
            check("登录后 Vue 工作台可见", True)
            brand = page.locator(".chat-header__brand")
            check(
                "Leon 品牌字呈主题蓝且连接状态紧随其旁",
                brand.locator("h1").evaluate(
                    "el => getComputedStyle(el).color !== 'rgb(16, 35, 61)'"
                )
                and brand.locator(".app-status").is_visible(),
            )
            stored_session = page.evaluate("() => localStorage.getItem('leon_session')")
            check("登录后创建并持久化会话", stored_session == FAKE_SESSION_ID, repr(stored_session))
            check(
                "健康检查经历未授权与 token 两条路径",
                sum(call["path"] == "/api/health" for call in gateway.calls) >= 2,
            )

            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                ["tool.started", {"tool_name": "fake_tool", "input": {"kind": "smoke"}}],
            )
            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                ["tool.finished", {"tool_name": "fake_tool", "ok": True}],
            )
            tool_card = page.locator(".message-tool").last
            tool_card.wait_for(state="visible")
            check(
                "工具状态显示在助手气泡内",
                "调用工具" in tool_card.inner_text()
                and "已完成" in tool_card.inner_text()
                and page.locator(".composer-notice").count() == 0,
            )
            timeline_toggle = page.get_by_role("button", name="运行记录")
            timeline_toggle.click()
            timeline_panel = page.locator("#timeline-panel")
            timeline_panel.wait_for(state="visible")
            timeline_entries = timeline_panel.locator(".timeline-entry")
            check(
                "运行记录收集 SSE 决策事件",
                timeline_entries.count() >= 3
                and "工具开始" in timeline_panel.inner_text()
                and "fake_tool" in timeline_panel.inner_text(),
            )
            timeline_panel.get_by_role("button", name="清空").click()
            check(
                "Timeline 支持清空并保留空态",
                timeline_panel.get_by_text("暂无事件").is_visible(),
            )
            timeline_panel.get_by_role("button", name="关闭时间线").click()

            # Reload with token+session intact to exercise the restore path:
            # GET /sessions/:id returns history with created_at, and gaps over
            # 10 minutes must render centered time dividers.
            page.reload(wait_until="domcontentloaded")
            page.get_by_text("已连接").wait_for(state="visible")
            page.locator(".message-row", has_text="历史消息一").wait_for(state="visible")
            time_dividers = page.locator(".time-divider")
            check(
                "超过 10 分钟间隔的历史消息插入时间分割线",
                time_dividers.count() >= 2,
                f"dividers={time_dividers.count()}",
            )

            gateway.append_session_message("user", "刷新期间的问题")
            gateway.active_turn = {"retry": False}
            page.reload(wait_until="domcontentloaded")
            page.get_by_text("已连接").wait_for(state="visible")
            thinking = page.locator(".thinking").last
            thinking.wait_for(state="visible")
            check(
                "刷新时恢复在途回复和三点思考态",
                thinking.locator("i").count() == 3
                and page.get_by_role("button", name="停止生成").is_visible(),
            )

            gateway.active_turn = None
            gateway.append_session_message("assistant", "刷新完成后保留的回复")
            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                ["assistant.completed", {"content": "刷新完成后保留的回复"}],
            )
            page.get_by_text("刷新完成后保留的回复").wait_for(state="visible")
            page.reload(wait_until="domcontentloaded")
            page.get_by_text("刷新完成后保留的回复").wait_for(state="visible")
            check(
                "在途回复完成后再次刷新仍保留",
                page.locator(".thinking").count() == 0
                and page.get_by_role("button", name="发送消息").is_visible(),
            )

            gateway.append_session_message("user", "需要直接停止的问题")
            gateway.active_turn = {"retry": False}
            gateway.cancel_post_405 = True
            page.reload(wait_until="domcontentloaded")
            stop_button = page.get_by_role("button", name="停止生成")
            stop_style = stop_button.evaluate(
                "el => ({ color: getComputedStyle(el).backgroundColor, "
                "image: getComputedStyle(el).backgroundImage })"
            )
            check(
                "停止生成按钮使用主题危险色而不是黑色",
                "38, 57, 79" not in stop_style["color"]
                and "38, 57, 79" not in stop_style["image"]
                and "linear-gradient" in stop_style["image"],
                repr(stop_style),
            )
            stop_button.click()
            page.get_by_role("button", name="发送消息").wait_for(state="visible")
            page.wait_for_timeout(200)
            cancel_calls = [
                call
                for call in gateway.calls
                if call["path"] == f"/api/agent/sessions/{FAKE_SESSION_ID}/cancel"
            ]
            check(
                "停止生成可直接打断并兼容旧服务的 405",
                len(cancel_calls) >= 2
                and cancel_calls[-2]["method"] == "POST"
                and cancel_calls[-1]["method"] == "DELETE",
                repr(cancel_calls[-2:]),
            )
            gateway.cancel_post_405 = False

            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                [
                    "assistant.completed",
                    {
                        "content": "带用量的回复",
                        "elapsed_ms": 4210,
                        "model": "fake-model-x",
                        "usage": {"input_tokens": 1234, "output_tokens": 567},
                    },
                ],
            )
            meta_row = page.locator(".message-row", has_text="带用量的回复").locator(
                ".message-toolbar"
            )
            meta_text = meta_row.inner_text()
            check(
                "assistant.completed 的 tokens 上屏且不显示模型名",
                "1.8k tokens" in meta_text
                and "↑" not in meta_text
                and "↓" not in meta_text
                and "fake-model-x" not in meta_text,
                meta_text.replace("\n", " "),
            )

            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                ["assistant.delta", {"delta": "流式"}],
            )
            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                ["assistant.delta", {"delta": "片段上屏"}],
            )
            streaming_bubble = page.locator(
                ".message-row[data-role='agent'] .message-content", has_text="流式片段上屏"
            )
            streaming_bubble.wait_for(state="visible")
            check(
                "assistant.delta 流式片段实时上屏",
                streaming_bubble.locator("xpath=ancestor::div[contains(@class, 'message-bubble')]")
                .get_attribute("data-status")
                == "streaming",
            )
            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                [
                    "assistant.completed",
                    {"content": "流式片段上屏", "usage": {"input_tokens": 9, "output_tokens": 9}},
                ],
            )

            mic_button = page.get_by_role("button", name="语音输入")
            mic_button.wait_for(state="visible")
            mic_button.click()
            page.get_by_role("button", name="停止录音并识别").wait_for(state="visible")
            page.wait_for_timeout(400)
            page.get_by_role("button", name="停止录音并识别").click()
            composer = page.locator("textarea[placeholder='有什么想聊的？']")
            page.wait_for_function(
                "() => document.querySelector(\"textarea[placeholder='有什么想聊的？']\").value"
                ".includes('这是语音识别出来的文字')"
            )
            check(
                "ASR 录音上传后回填输入框且不自动发送",
                "这是语音识别出来的文字" in composer.input_value(),
                composer.input_value(),
            )
            composer.fill("")

            composer = page.locator("textarea[placeholder='有什么想聊的？']")
            composer.fill("/nsfw --model ")
            suggestions = page.locator(".mode-suggestions .mode-suggestion")
            suggestions.first.wait_for(state="visible")
            suggestion_count = suggestions.count()
            target_name = suggestions.nth(1).inner_text().splitlines()[0].strip()
            composer.press("ArrowDown")
            composer.press("Enter")
            selected_value = composer.input_value()
            check(
                "`/nsfw --model` 拉取目录并可用键盘选中",
                suggestion_count >= 2 and target_name in selected_value,
                f"候选={suggestion_count}，输入框={selected_value!r}",
            )
            check(
                "模式目录请求被 fake Gateway 接管",
                any(call["path"] == "/api/image-modes" for call in gateway.calls),
            )

            composer.fill("第一行\n第二行\n第三行\n第四行\n第五行\n第六行")
            expanded_height = float(
                composer.evaluate("element => parseFloat(getComputedStyle(element).height)")
            )
            composer.fill("")
            collapsed_height = float(
                composer.evaluate("element => parseFloat(getComputedStyle(element).height)")
            )
            check(
                "多行输入自动增高并限制在五行预算",
                42 < expanded_height <= 120.5 and collapsed_height <= 43,
                f"展开={expanded_height}px，清空={collapsed_height}px",
            )

            agent_rows = page.locator(".message-row[data-role='agent']")
            agent_count_before = agent_rows.count()
            composer.fill("普通 provider-free 消息")
            composer.press("Enter")
            page.wait_for_function(
                "([expected, before]) => document.querySelectorAll("
                "\".message-row[data-role='agent']\").length > before && Array.from("
                "document.querySelectorAll("
                "\".message-row[data-role='agent'] .message-content\""
                ")).some((node) => node.textContent.includes(expected))",
                arg=["本地回复", agent_count_before],
            )
            agent_bubble = page.locator(".message-row[data-role='agent'] .message-content").last
            agent_bubble.wait_for(state="visible")
            check(
                "普通消息发送并显示助手回复",
                "本地回复" in agent_bubble.inner_text(),
                agent_bubble.inner_text()[:80],
            )
            message_calls = [
                call
                for call in gateway.calls
                if call["path"] == f"/api/agent/sessions/{FAKE_SESSION_ID}/messages"
            ]
            check(
                "普通消息 POST body 保持用户原文",
                bool(message_calls)
                and message_calls[-1]["body"].get("content") == "普通 provider-free 消息",
                repr(message_calls[-1]["body"] if message_calls else None),
            )

            user_row = page.locator(".message-row[data-role='user']").last
            user_toolbar = user_row.locator(".message-toolbar")
            check(
                "用户气泡提供复制、重试和编辑",
                user_toolbar.get_by_role("button", name="复制").is_visible()
                and user_toolbar.get_by_role("button", name="重试").is_visible()
                and user_toolbar.get_by_role("button", name="编辑").is_visible(),
            )
            user_toolbar.get_by_role("button", name="编辑").click()
            edit_dialog = page.locator(".message-edit-dialog")
            edit_dialog.wait_for(state="visible")
            user_editor = edit_dialog.get_by_label("消息内容")
            messages_panel = page.locator(".messages-panel")
            check(
                "编辑使用独立弹窗且不改变气泡布局",
                user_row.locator(".message-bubble").is_visible()
                and edit_dialog.is_visible()
                and messages_panel.evaluate("el => el.scrollWidth <= el.clientWidth + 1"),
            )
            user_editor.fill("编辑后的用户消息")
            edit_dialog.get_by_role("button", name="保存").click()
            edit_dialog.wait_for(state="hidden")
            check("用户消息可以原位编辑", "编辑后的用户消息" in user_row.inner_text())
            agent_count_before_retry = page.locator(".message-row[data-role='agent']").count()
            page.evaluate("() => { window.__leonCompleteRetryBeforePost = true; }")
            user_row.get_by_role("button", name="重试").click()
            retried_agent = page.locator(".message-row[data-role='agent']").last
            retried_agent.get_by_text("这是重试后的本地回复。").wait_for(state="visible")
            revision_button = retried_agent.locator(".message-revision button")
            check(
                "重试覆盖当前助手气泡并生成版本入口",
                "2 / 2" in revision_button.inner_text()
                and gateway.calls[-1]["body"].get("retry") is True,
                repr(gateway.calls[-1]),
            )
            check(
                "SSE 先完成时 HTTP 兜底不会追加重复气泡",
                page.locator(".message-row[data-role='agent']").count()
                == agent_count_before_retry,
            )
            revision_button.click()
            check(
                "版本入口可顺序切换到旧回答",
                "provider-free 的本地回复" in retried_agent.inner_text(),
            )
            revision_button.click()
            check(
                "版本入口可切回最新回答",
                "重试后的本地回复" in retried_agent.inner_text(),
            )
            page.reload(wait_until="domcontentloaded")
            page.get_by_text("已连接").wait_for(state="visible")
            retried_agent = page.locator(".message-row[data-role='agent']").last
            retried_agent.wait_for(state="visible")
            page.get_by_text("这是重试后的本地回复。").wait_for(state="visible")
            revision_button = retried_agent.locator(".message-revision button")
            check(
                "重试版本数量刷新后仍然正确",
                "2 / 2" in revision_button.inner_text(),
                revision_button.inner_text(),
            )
            revision_button.click()
            check(
                "刷新后仍可切换到历史回答",
                "provider-free 的本地回复" in retried_agent.inner_text(),
            )
            revision_button.click()

            tts_calls_before = sum(
                call["path"] == "/api/agent/tts" for call in gateway.calls
            )
            speech_control = retried_agent.locator(".message-speak")
            speech_control.click()
            page.wait_for_function(
                "el => el.getAttribute('aria-label') !== '生成中…'",
                arg=speech_control.element_handle(),
            )
            if speech_control.get_attribute("aria-label") == "停止":
                speech_control.click()
            page.wait_for_function(
                "el => el.getAttribute('aria-label') === '朗读'",
                arg=speech_control.element_handle(),
            )
            speech_control.click()
            page.wait_for_function(
                "el => el.getAttribute('aria-label') !== '生成中…'",
                arg=speech_control.element_handle(),
            )
            tts_calls_after = sum(
                call["path"] == "/api/agent/tts" for call in gateway.calls
            )
            check(
                "同一文字和音色重复朗读复用前端缓存",
                tts_calls_after - tts_calls_before == 1,
                f"before={tts_calls_before}, after={tts_calls_after}",
            )

            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                [
                    "image.completed",
                    {"job_id": "live-image-job", "image_url": "/api/fake-image"},
                ],
            )
            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                [
                    "image.completed",
                    {"job_id": "live-image-job-2", "image_url": "/api/fake-image-2"},
                ],
            )
            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                [
                    "assistant.notice",
                    {
                        "content": "图片生成好了，2 张图在呢，赶紧点开看！",
                        "job_ids": ["live-image-job", "live-image-job-2"],
                    },
                ],
            )
            image_result = page.locator(".message-row[data-kind='image-result']").last
            image_result.wait_for(state="visible")
            check(
                "图片与完成文案合并且不显示文本工具栏",
                image_result.locator(".markdown-image").count() == 2
                and "赶紧点开看" in image_result.inner_text()
                and image_result.locator(".message-toolbar").count() == 0,
            )
            image_result.locator(".markdown-image-link").first.click()
            chat_viewer = page.locator(".image-viewer")
            chat_viewer.wait_for(state="visible")
            chat_counter = chat_viewer.locator("figcaption")
            check(
                "聊天全屏包含当前气泡的全部图片",
                "1 / 2" in chat_counter.inner_text()
                and chat_viewer.get_by_role("button", name="上一张").is_visible()
                and chat_viewer.get_by_role("button", name="下一张").is_visible(),
                chat_counter.inner_text(),
            )
            page.mouse.move(320, 422)
            page.mouse.down()
            page.mouse.move(70, 422, steps=5)
            page.mouse.up()
            chat_counter.get_by_text("2 / 2", exact=False).wait_for(state="visible")
            check("聊天全屏支持向左滑动切到下一张", "2 / 2" in chat_counter.inner_text())
            chat_viewer.get_by_role("button", name="关闭").click()

            page.get_by_role("button", name="任务").click()
            page.locator(".page-panel[aria-label='生图任务']").wait_for(state="visible")
            check(
                "任务页复用固定公共头部",
                page.locator(".chat-header").is_visible()
                and page.get_by_role("button", name="刷新任务").is_visible()
                and page.locator(
                    ".page-panel[aria-label='生图任务'] > .page-panel__header"
                ).count()
                == 0,
            )
            task_card = page.locator(".task-card").first
            task_cards = page.locator(".task-card")
            task_thumbnail = task_card.locator(".task-card__thumbnail")
            task_boxes = [
                task_cards.nth(index).bounding_box() for index in range(task_cards.count())
            ]
            check(
                "完成任务直接展示可点击缩略图且移除任务详情",
                task_thumbnail.is_visible()
                and task_card.locator(".task-card__details").count() == 0
                and "任务详情" not in task_card.inner_text(),
            )
            check(
                "不同状态任务卡保持等高",
                len(task_boxes) >= 2
                and all(box is not None for box in task_boxes)
                and max(box["height"] for box in task_boxes if box is not None)
                - min(box["height"] for box in task_boxes if box is not None)
                <= 1,
                repr(task_boxes),
            )
            task_thumbnail.click()
            task_viewer = page.locator(".image-viewer")
            task_viewer.wait_for(state="visible")
            check("任务缩略图点击后进入全屏", True)
            task_viewer.locator(".image-viewer__close").click()

            page.get_by_role("button", name="图库").click()
            page.locator(".gallery-grid").wait_for(state="visible")
            check(
                "图库页复用固定公共头部",
                page.locator(".chat-header").is_visible()
                and page.get_by_role("button", name="刷新图库").is_visible(),
            )
            page.get_by_role("button", name="查看 测试图片").click()
            viewer = page.locator(".image-viewer")
            viewer.wait_for(state="visible")
            image_box = viewer.locator(".image-viewer__figure img").bounding_box()
            close_box = viewer.locator(".image-viewer__close").bounding_box()
            image_metrics = viewer.locator(".image-viewer__figure img").evaluate(
                "el => ({ naturalWidth: el.naturalWidth, naturalHeight: el.naturalHeight, "
                "clientWidth: el.clientWidth, clientHeight: el.clientHeight, "
                "objectFit: getComputedStyle(el).objectFit })"
            )
            viewer_metrics = viewer.evaluate(
                "el => ({ backgroundColor: getComputedStyle(el).backgroundColor, "
                "touchAction: getComputedStyle(el).touchAction, "
                "overflow: getComputedStyle(el).overflow })"
            )
            control_positions = viewer.locator(
                ".image-viewer__close, .image-viewer__nav"
            ).evaluate_all("els => els.map(el => getComputedStyle(el).position)")
            check(
                "全屏图片保持原比例并使用统一留边背景",
                image_box is not None
                and round(image_box["width"]) == 390
                and round(image_box["height"]) == 844
                and image_metrics["objectFit"] == "contain"
                and image_metrics["naturalWidth"] / image_metrics["naturalHeight"]
                != image_metrics["clientWidth"] / image_metrics["clientHeight"]
                and viewer_metrics["backgroundColor"] != "rgb(0, 0, 0)"
                and "22, 37, 55" in viewer_metrics["backgroundColor"]
                and viewer_metrics["touchAction"] == "none"
                and viewer_metrics["overflow"] == "hidden"
                and close_box is not None
                and close_box["y"] >= 20,
                f"图片={image_box}，比例={image_metrics}，查看器={viewer_metrics}，关闭={close_box}",
            )
            check(
                "全屏关闭与左右切图按钮固定在视口",
                len(control_positions) == 3
                and all(value == "fixed" for value in control_positions),
                repr(control_positions),
            )
            viewer.locator(".image-viewer__close").click()
            page.get_by_role("button", name="聊天").click()

            for index in range(24):
                page.evaluate(
                    "([event, data]) => window.__leonEmit(event, data)",
                    ["assistant.notice", {"content": f"滚动占位消息 {index + 1}"}],
                )
            messages_panel = page.locator(".messages-panel")
            page.locator(".message-row").nth(20).wait_for(state="visible")
            messages_panel.evaluate(
                "element => { const behavior = element.style.scrollBehavior; "
                "element.style.scrollBehavior = 'auto'; element.scrollTop = 0; "
                "element.style.scrollBehavior = behavior; "
                "element.dispatchEvent(new Event('scroll')); }"
            )
            jump_to_latest = page.get_by_role("button", name="回到最新消息")
            jump_to_latest.wait_for(state="visible")
            scroll_before = float(messages_panel.evaluate("element => element.scrollTop"))
            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                ["assistant.notice", {"content": "上滚后的新消息"}],
            )
            page.wait_for_timeout(50)
            scroll_after = float(messages_panel.evaluate("element => element.scrollTop"))
            check(
                "用户上滚后新消息不会强制拉回底部",
                scroll_after <= scroll_before + 2,
                f"before={scroll_before}，after={scroll_after}",
            )
            jump_to_latest.click()
            page.wait_for_function(
                "() => { const panel = document.querySelector('.messages-panel'); "
                "return panel && panel.scrollHeight - panel.scrollTop - panel.clientHeight <= 72; }"
            )
            check("回到最新按钮恢复自动跟随", not jump_to_latest.is_visible())

            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                ["agent.error", {"error": "HTTP 424 upstream_error\ntrace-id=fake"}],
            )
            error_details = page.locator(".message-error__details").last
            error_details.wait_for(state="visible")
            raw_error = error_details.locator(".message-error__raw")
            check(
                "错误气泡默认只显示摘要",
                not bool(error_details.evaluate("element => element.open"))
                and "查看错误详情" in error_details.inner_text(),
            )
            error_details.locator("summary").click()
            raw_error.wait_for(state="visible")
            check(
                "展开后可查看原始错误且保留重试入口",
                bool(error_details.evaluate("element => element.open"))
                and "HTTP 424 upstream_error" in raw_error.inner_text()
                and page.get_by_role("button", name="重试").last.is_visible(),
            )
            error_count = page.locator(".message-bubble[data-status='error']").count()
            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                ["agent.error", {"error": "HTTP 424 upstream_error\ntrace-id=fake"}],
            )
            page.wait_for_timeout(50)
            check(
                "相同失败事件只显示一个错误气泡",
                page.locator(".message-bubble[data-status='error']").count() == error_count,
            )

            voice_payload = {
                "clip_id": "clip-vue-e2e",
                "url": "/api/voice/clips/clip-vue-e2e",
                "text": "这是 fake voice.ready 消息",
                "voice_id": FAKE_VOICE_ID,
                "voice_name": "测试音色",
                "bytes": 128,
            }
            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                ["voice.ready", voice_payload],
            )
            voice_bubble = page.locator(".voice-bubble").last
            voice_bubble.wait_for(state="visible")
            check(
                "voice.ready 事件追加语音气泡",
                "fake voice.ready" in voice_bubble.inner_text()
                and voice_bubble.locator(".voice-player").is_visible()
                and voice_bubble.locator("audio.voice-bubble__audio").count() == 1
                and voice_bubble.locator("audio.voice-bubble__audio").evaluate(
                    "el => getComputedStyle(el).display === 'none'"
                ),
            )
            voice_toggle_button = voice_bubble.locator(".voice-player__toggle")
            if voice_toggle_button.get_attribute("aria-label") == "播放语音":
                voice_toggle_button.click()
            page.wait_for_function(
                "el => el.getAttribute('aria-label') === '暂停语音'",
                arg=voice_toggle_button.element_handle(),
            )
            check(
                "可见气泡播放器直接控制自身音频",
                voice_bubble.locator(".voice-player").get_attribute("data-playing") == "true",
            )
            voice_toggle_button.click()
            check(
                "语音 clip 请求保持在同源 fake Gateway",
                any(call["path"] == "/api/voice/clips/clip-vue-e2e" for call in gateway.calls),
            )

            page.get_by_role("button", name="设置").click()
            page.locator(".settings-panel").wait_for(state="visible")
            check(
                "设置页复用固定公共头部且没有独立页面标题",
                page.locator(".chat-header").is_visible()
                and page.locator(".settings-panel > .page-panel__header").count() == 0,
            )
            nav_box = page.locator(".bottom-nav").bounding_box()
            check(
                "底部导航固定在视口底部区域",
                nav_box is not None and nav_box["y"] + nav_box["height"] >= 820,
                repr(nav_box),
            )
            check(
                "底部导航与历史按钮不会触发双击缩放",
                page.locator(".bottom-nav button").first.evaluate(
                    "el => getComputedStyle(el).touchAction === 'manipulation'"
                )
                and page.locator(".timeline-toggle").evaluate(
                    "el => getComputedStyle(el).touchAction === 'manipulation'"
                ),
            )
            check(
                "设置页底部提供大号退出登录按钮",
                page.locator(".logout-big").is_visible()
                and "退出登录" in page.locator(".logout-big").inner_text(),
            )
            page.locator(".logout-big").click()
            logout_confirm = page.locator(".confirm-dialog")
            logout_confirm.wait_for(state="visible")
            check(
                "退出登录需要二次确认",
                "确认退出登录" in logout_confirm.inner_text()
                and logout_confirm.get_by_role("button", name="确认退出").is_visible(),
            )
            logout_confirm.get_by_role("button", name="取消").click()
            logout_confirm.wait_for(state="hidden")
            voice_card = page.locator(".voice-settings")
            model_get_count = sum(
                call["method"] == "GET"
                and call["path"] == f"/api/agent/sessions/{FAKE_SESSION_ID}/model"
                for call in gateway.calls
            )
            page.get_by_role("button", name="聊天").click()
            page.get_by_role("button", name="设置").click()
            page.locator(".settings-panel").wait_for(state="visible")
            page.wait_for_timeout(100)
            cached_model_get_count = sum(
                call["method"] == "GET"
                and call["path"] == f"/api/agent/sessions/{FAKE_SESSION_ID}/model"
                for call in gateway.calls
            )
            check(
                "再次进入设置复用模型缓存且不自动刷新",
                model_get_count == 1 and cached_model_get_count == model_get_count,
                f"first={model_get_count}, cached={cached_model_get_count}",
            )
            page.get_by_label("刷新模型目录").click()
            page.wait_for_timeout(100)
            refreshed_model_get_count = sum(
                call["method"] == "GET"
                and call["path"] == f"/api/agent/sessions/{FAKE_SESSION_ID}/model"
                for call in gateway.calls
            )
            check(
                "手动刷新模型目录会重新请求",
                refreshed_model_get_count == cached_model_get_count + 1,
                f"cached={cached_model_get_count}, refreshed={refreshed_model_get_count}",
            )
            voice_card = page.locator(".voice-settings")
            voice_before = voice_card.bounding_box()
            page.get_by_label("模型 ID").click()
            page.locator(".model-list").wait_for(state="visible")
            voice_after = voice_card.bounding_box()
            check(
                "模型目录悬浮展开且不推动语音卡片",
                voice_before is not None
                and voice_after is not None
                and abs(voice_before["y"] - voice_after["y"]) <= 1,
                f"before={voice_before}, after={voice_after}",
            )
            page.get_by_label("模型 ID").press("Escape")
            voice_toggle = page.get_by_role("button", name="选择音色")
            voice_toggle.wait_for(state="visible")
            check(
                "模型和音色只在选择框内显示当前值",
                "当前：" not in page.locator(".settings-panel").inner_text()
                and "测试音色" in voice_toggle.inner_text()
                and voice_toggle.locator(".lucide-audio-lines").count() == 1,
                voice_toggle.inner_text().replace("\n", " "),
            )
            check("语音列表默认收起", not page.locator(".voice-list").is_visible())
            logout_before = page.locator(".logout-big").bounding_box()
            voice_toggle.click()
            voice_list = page.locator(".voice-list")
            voice_list.wait_for(state="visible")
            voice_panel = page.locator(".voice-catalog-panel")
            voice_header = page.locator(".voice-catalog-panel__header")
            voice_pager = page.locator(".voice-pager")
            logout_after = page.locator(".logout-big").bounding_box()
            check(
                "音色目录悬浮展开且退出按钮保持在底部",
                logout_before is not None
                and logout_after is not None
                and abs(logout_before["y"] - logout_after["y"]) <= 1,
                f"before={logout_before}, after={logout_after}",
            )
            voice_panel_box = voice_panel.bounding_box()
            voice_card_box = voice_card.bounding_box()
            check(
                "音色弹层宽度合理、限高且不覆盖底部导航",
                voice_panel_box is not None
                and voice_card_box is not None
                and voice_panel_box["width"] > voice_card_box["width"]
                and 380 < voice_panel_box["height"] <= 490
                and voice_panel_box["y"] + voice_panel_box["height"] < 760
                and voice_panel.evaluate("el => getComputedStyle(el).overflow === 'hidden'"),
                f"panel={voice_panel_box}, card={voice_card_box}",
            )
            header_before = voice_header.bounding_box()
            pager_before = voice_pager.bounding_box()
            voice_list.evaluate("el => { el.scrollTop = el.scrollHeight; }")
            header_after = voice_header.bounding_box()
            pager_after = voice_pager.bounding_box()
            check(
                "音色只滚动中间列表且页签分页固定",
                voice_list.evaluate("el => getComputedStyle(el).overflowY === 'auto'")
                and header_before is not None
                and header_after is not None
                and pager_before is not None
                and pager_after is not None
                and abs(header_before["y"] - header_after["y"]) <= 1
                and abs(pager_before["y"] - pager_after["y"]) <= 1,
            )
            voice_tabs = page.locator(".voice-tabs button")
            check(
                "音色目录提供全部/收藏页签",
                voice_tabs.count() == 2
                and "全部" in voice_tabs.nth(0).inner_text()
                and voice_tabs.nth(1).inner_text().strip() == "收藏",
            )
            voice_list.locator(".voice-option__star").first.click()
            voice_tabs.nth(1).click()
            favorite_rows = voice_list.locator(".voice-option")
            favorite_row_box = favorite_rows.first.bounding_box()
            check(
                "收藏只有一条时不会拉伸吃满整页",
                favorite_rows.count() == 1
                and favorite_row_box is not None
                and favorite_row_box["height"] <= 54
                and voice_list.evaluate("el => getComputedStyle(el).alignContent === 'start'"),
                f"rows={favorite_rows.count()}, box={favorite_row_box}",
            )
            voice_tabs.nth(0).click()
            voice_search = page.get_by_label("搜索音色")
            voice_search.fill("测试音色")
            search_rows = voice_list.locator(".voice-option")
            search_row_box = search_rows.first.bounding_box()
            check(
                "搜索只有一条时保持紧凑行高",
                search_rows.count() == 1
                and search_row_box is not None
                and search_row_box["height"] <= 54,
                f"rows={search_rows.count()}, box={search_row_box}",
            )
            voice_search.fill("")
            autoplay_input = page.locator(".settings-toggle input")
            check(
                "自动朗读呈现为滑动开关",
                autoplay_input.evaluate("el => getComputedStyle(el).opacity === '0'"),
            )
            check(
                "已移除 JOK 音色并保留可用音色",
                "JOK" not in voice_list.inner_text()
                and page.get_by_role("button", name="试听 测试音色").is_visible(),
            )
            page.get_by_role("button", name="试听 测试音色").click()
            page.get_by_role("button", name="停止试听").wait_for(state="visible")
            check("试听按钮进入播放状态", True)
            page.get_by_role("button", name="停止试听").click()
            page.get_by_role("button", name="关闭音色选择").last.click()
            voice_panel.wait_for(state="hidden")
            check("音色弹层可用右上角按钮关闭", True)
            voice_toggle.click()
            voice_panel.wait_for(state="visible")
            page.keyboard.press("Escape")
            voice_panel.wait_for(state="hidden")
            check("音色弹层可用 Escape 关闭", True)
            voice_toggle.click()
            voice_panel.wait_for(state="visible")
            page.locator(".voice-catalog-backdrop").click(position={"x": 4, "y": 4})
            voice_panel.wait_for(state="hidden")
            check("音色弹层可点击空白遮罩关闭", True)
            autoplay_input.evaluate("el => { if (!el.checked) el.click(); }")
            page.get_by_role("button", name="聊天").click()
            page.get_by_role("heading", name="Leon").wait_for(state="visible")

            latest_tts_before = sum(
                call["path"] == "/api/agent/tts" for call in gateway.calls
            )
            latest_image_one = f"{base_url}/api/fake-image?filename=latest-1.png"
            latest_image_two = f"{base_url}/api/fake-image-2?filename=latest-2.png"
            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                ["tool.started", {"tool_name": "get_latest_images", "input": {"limit": 2}}],
            )
            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                [
                    "tool.finished",
                    {
                        "tool_name": "get_latest_images",
                        "ok": True,
                        "output": {
                            "ok": True,
                            "items": [
                                {"image_url": latest_image_one},
                                {"image_url": latest_image_two},
                            ],
                        },
                    },
                ],
            )
            page.evaluate(
                "([event, data]) => window.__leonEmit(event, data)",
                [
                    "assistant.completed",
                    {
                        "content": (
                            "最新的 2 张图：\n"
                            "1. [查看图片 1（写实基础）]"
                            f"({latest_image_one})\n"
                            "2. [查看图片 2（蒂法增强）]"
                            f"({latest_image_two})"
                        )
                    },
                ],
            )
            latest_images_row = page.locator(
                ".message-row[data-kind='image-result']", has_text="读取最近图片"
            ).last
            latest_images_row.wait_for(state="visible")
            latest_image_count = latest_images_row.locator(".markdown-image").count()
            latest_image_sources = latest_images_row.locator(".markdown-image").evaluate_all(
                "nodes => nodes.map(node => node.getAttribute('src'))"
            )
            latest_image_metrics = latest_images_row.locator(".markdown-image").first.evaluate(
                "node => ({ naturalWidth: node.naturalWidth, naturalHeight: node.naturalHeight, "
                "clientWidth: node.clientWidth, clientHeight: node.clientHeight, "
                "objectFit: getComputedStyle(node).objectFit })"
            )
            check(
                "查询最近图片直接渲染图片而不是查看链接列表",
                latest_image_count == 2
                and "查看图片" not in latest_images_row.inner_text(),
                f"images={latest_image_count}, sources={latest_image_sources}, text="
                f"{latest_images_row.inner_text().replace(chr(10), ' ')}",
            )
            check(
                "聊天图片保持原始比例且不裁切",
                latest_image_sources == [latest_image_one, latest_image_two]
                and latest_image_metrics["naturalWidth"] == 1536
                and latest_image_metrics["naturalHeight"] == 2500
                and latest_image_metrics["naturalWidth"] > 0
                and latest_image_metrics["naturalHeight"] > 0
                and abs(
                    latest_image_metrics["clientWidth"] / latest_image_metrics["clientHeight"]
                    - latest_image_metrics["naturalWidth"] / latest_image_metrics["naturalHeight"]
                )
                < 0.02
                and latest_image_metrics["objectFit"] == "contain",
                f"sources={latest_image_sources}, metrics={latest_image_metrics}",
            )
            page.wait_for_timeout(200)
            latest_tts_after = sum(
                call["path"] == "/api/agent/tts" for call in gateway.calls
            )
            check(
                "图片查询结果不会自动朗读查看图片文案",
                latest_tts_after == latest_tts_before
                and latest_images_row.locator(".message-speak").count() == 0,
                f"before={latest_tts_before}, after={latest_tts_after}",
            )
            if args.screenshot:
                page.screenshot(path=f"{args.screenshot}.latest.png", full_page=True)

            gateway.token_valid = False
            page.evaluate("() => window.__leonFailEvents(true)")
            login_heading.wait_for(state="visible")
            check(
                "SSE token 失效后停止重连并返回登录页",
                "登录已失效" in page.locator(".form-error").inner_text()
                and page.evaluate("() => localStorage.getItem('leon_token')") is None
                and page.evaluate("() => localStorage.getItem('leon_session')") is None,
            )

            external_errors = [
                error
                for error in [*page_errors, *console_errors]
                if "favicon" not in error.lower()
                # The first health probe intentionally exercises the login
                # branch and therefore produces one expected 401 console
                # entry in Chromium.
                and "status of 401 (unauthorized)" not in error.lower()
                # The cancel compatibility check deliberately makes POST
                # return 405 before the client retries with DELETE.
                and "status of 405 (method not allowed)" not in error.lower()
            ]
            check("运行期无 Vue 页面错误", not external_errors, "; ".join(external_errors[:3]))
            if args.screenshot:
                page.screenshot(path=args.screenshot, full_page=True)
        except Exception as exc:  # noqa: BLE001 - preserve all smoke failures in summary
            check("浏览器 smoke 未抛出异常", False, repr(exc))
        finally:
            context.close()
            browser.close()

    failed = [item for item in checks if not item[1]]
    print(f"\n{'=' * 60}\n{len(checks) - len(failed)}/{len(checks)} 通过")
    if failed:
        print("失败项：")
        for name, _, detail in failed:
            print(f"  - {name}  {detail}")
    return 1 if failed else 0


def main() -> int:
    args = _parser().parse_args()
    handle: ServerHandle | None = None
    try:
        handle = _start_server(args)
        return run_browser_check(handle.base_url, args)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"无法准备 Vue 浏览器 smoke：{exc}", file=sys.stderr)
        return 2
    finally:
        if handle is not None:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
