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
                    "messages": [
                        {"role": "user", "content": "历史消息一", "created_at": 1_000},
                        {
                            "role": "assistant",
                            "content": "历史回复一",
                            "created_at": 1_000_000,
                        },
                    ],
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
                    "tasks": [],
                    "images": [
                        {
                            "job_id": "fake-image-job",
                            "image_url": "/api/fake-image",
                            "source_text": "测试图片",
                            "created_at": 1,
                        }
                    ],
                    "errors": {},
                },
            )
            return
        if path == "/api/fake-image" and method == "GET":
            fake_svg = (
                b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="3">'
                b'<rect width="4" height="3" fill="#2783de"/></svg>'
            )
            route.fulfill(status=200, content_type="image/svg+xml", body=fake_svg)
            return
        if path == f"{session_prefix}/messages" and method == "POST":
            _json_response(
                route,
                {
                    "session_id": FAKE_SESSION_ID,
                    "answer": "这是 provider-free 的本地回复。",
                    "ok": True,
                },
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
            _command("uv"),
            "run",
            "leon-server",
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

            page.locator("#token").fill(FAKE_TOKEN)
            page.get_by_role("button", name="进入").click()
            page.get_by_role("heading", name="Leon").wait_for(state="visible")
            page.get_by_text("已连接").wait_for(state="visible")
            check("登录后 Vue 工作台可见", True)
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
                "assistant.completed 的 tokens 与模型名上屏",
                "↑1.2k" in meta_text and "↓567" in meta_text and "fake-model-x" in meta_text,
                meta_text.replace("\n", " "),
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

            page.get_by_role("button", name="图库").click()
            page.get_by_role("heading", name="图库").wait_for(state="visible")
            page.get_by_role("button", name="查看 测试图片").click()
            viewer = page.locator(".image-viewer")
            viewer.wait_for(state="visible")
            image_box = viewer.locator(".image-viewer__figure img").bounding_box()
            close_box = viewer.locator(".image-viewer__close").bounding_box()
            check(
                "全屏图片横向铺满并保留关闭安全区",
                image_box is not None
                and round(image_box["width"]) == 390
                and close_box is not None
                and close_box["y"] >= 20,
                f"图片={image_box}，关闭={close_box}",
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
                and voice_bubble.locator("audio.voice-bubble__audio").count() == 1,
            )
            check(
                "语音 clip 请求保持在同源 fake Gateway",
                any(call["path"] == "/api/voice/clips/clip-vue-e2e" for call in gateway.calls),
            )

            page.get_by_role("button", name="设置").click()
            page.get_by_role("heading", name="设置").wait_for(state="visible")
            voice_toggle = page.get_by_role("button", name="选择音色")
            voice_toggle.wait_for(state="visible")
            check("语音列表默认收起", not page.locator(".voice-list").is_visible())
            voice_toggle.click()
            voice_list = page.locator(".voice-list")
            voice_list.wait_for(state="visible")
            check(
                "已移除 JOK 音色并保留可用音色",
                "JOK" not in voice_list.inner_text()
                and page.get_by_role("button", name="试听 测试音色").is_visible(),
            )
            page.get_by_role("button", name="试听 测试音色").click()
            page.get_by_role("button", name="停止试听").wait_for(state="visible")
            check("试听按钮进入播放状态", True)
            page.get_by_role("button", name="停止试听").click()
            page.get_by_role("button", name="聊天").click()
            page.get_by_role("heading", name="Leon").wait_for(state="visible")

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
