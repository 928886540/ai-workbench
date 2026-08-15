"""Interactive `leon` command."""

from __future__ import annotations

import argparse
import io
import sys
import threading
from collections.abc import Sequence
from contextlib import nullcontext
from contextvars import ContextVar
from pathlib import Path
from time import monotonic

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from workbench_core.agent import AgentEvent, AgentResult, ToolStep
from workbench_core.agent.runtime import (
    AgentCancelled,
    cancellation_scope,
    current_cancel_event,
)
from workbench_core.config import Settings, get_settings, reset_settings_cache
from workbench_core.llm import LLMClient

from leon_agent.agent import LeonAgent
from leon_agent.config import LeonSettings
from leon_agent.image_modes import format_mode_catalog, parse_nsfw_command
from leon_agent.leon_client import LeonImageClient
from leon_agent.models import model_provider_scope, resolve_model_id
from leon_agent.session import SessionStore
from leon_agent.tools import create_leon_tools

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.widgets import Frame, Label, TextArea
except ModuleNotFoundError:  # pragma: no cover - legacy prompt fallback remains usable
    Application = None
    WordCompleter = None
    KeyBindings = None
    HSplit = None
    Layout = None
    Dimension = None
    InMemoryHistory = None
    Frame = None
    Label = None
    TextArea = None


_ACTIVE_TURN: ContextVar[tuple[int, threading.Event] | None] = ContextVar(
    "leon_cli_active_turn",
    default=None,
)

_CLI_COMMANDS = [
    "/help",
    "/new",
    "/history",
    "/status",
    "/model",
    "/clear",
    "/nsfw",
    "/exit",
    "/quit",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chat with Leon Agent and use Leon image tools")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["resume"],
        help="Command: resume an existing conversation",
    )
    parser.add_argument("resume_session", nargs="?", help="Session id used by `leon resume`")
    parser.add_argument("--once", help="Run one message and exit")
    parser.add_argument("--session", help="Resume an existing session id")
    parser.add_argument("--new", action="store_true", help="Always create a new session")
    parser.add_argument("--backend-url", help="Override LEON_BACKEND_URL")
    parser.add_argument(
        "--public-image-base-url",
        help="Override LEON_PUBLIC_IMAGE_BASE_URL used to build absolute image links",
    )
    parser.add_argument("--plugin-dir", help="Override LEON_PLUGIN_DIR")
    parser.add_argument("--db", help="Override LEON_SESSION_DB")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "resume":
        if not args.resume_session:
            parser.error("leon resume requires a session id")
        if args.session:
            parser.error("use either `leon resume <id>` or `--session <id>`, not both")
        if args.new:
            parser.error("`leon resume` cannot be combined with `--new`")
        args.session = args.resume_session
    return args


class TerminalChatUI:
    """Fullscreen chat surface with explicit turn ownership and cancellation."""

    _MAX_BLOCKS = 240
    _IDLE_STATUS = "● 就绪 · Enter 发送 · Ctrl+C 取消 · Ctrl+Q 退出"

    def __init__(self, owner: LeonConsole) -> None:
        if Application is None:
            raise RuntimeError("prompt_toolkit is not installed")
        self.owner = owner
        self.blocks: list[str] = []
        self.lock = threading.RLock()
        self.busy = False
        self.status_text = self._IDLE_STATUS
        self._generation = 0
        self._active_cancel_event: threading.Event | None = None
        self._active_thread: threading.Thread | None = None
        self._exit_requested = False
        self._started_at: float | None = None

        self.output = TextArea(
            text="",
            read_only=True,
            scrollbar=True,
            wrap_lines=True,
        )
        input_kwargs = {
            "height": 1,
            "prompt": "❯ ",
            "multiline": False,
            "accept_handler": self._accept,
        }
        if InMemoryHistory is not None:
            input_kwargs["history"] = InMemoryHistory()
        if WordCompleter is not None:
            input_kwargs["completer"] = WordCompleter(_CLI_COMMANDS, sentence=True)
        self.input = TextArea(**input_kwargs)
        self.header = Label(self._header_text)
        self.status = Label(self._status_line)
        self.footer = Label(
            "  Enter 发送   ·   Ctrl+C 取消当前轮   ·   Ctrl+Q 取消并退出   ·   Ctrl+L 清屏"
        )
        key_bindings = KeyBindings()

        @key_bindings.add("c-c")
        def _(event) -> None:  # noqa: ANN001
            self._handle_interrupt(event, exit_after=False)

        @key_bindings.add("c-q")
        def _(event) -> None:  # noqa: ANN001
            self._handle_interrupt(event, exit_after=True)

        @key_bindings.add("c-l")
        def _(event) -> None:  # noqa: ANN001
            self.clear_output()
            self._set_status(self.status_text)

        root = HSplit(
            [
                Frame(self.header, title="🦁 LEON AGENT · terminal cockpit"),
                Frame(
                    self.output,
                    title="💬 会话滚动区",
                    height=Dimension(weight=1),
                ),
                self.status,
                Frame(self.input, title="⌨ 输入 · 支持 /help /model /new /clear"),
                self.footer,
            ]
        )
        self.app = Application(
            layout=Layout(root, focused_element=self.input),
            key_bindings=key_bindings,
            full_screen=True,
            mouse_support=True,
        )

    @staticmethod
    def available() -> bool:
        return Application is not None

    def _header_text(self) -> str:
        model = getattr(self.owner, "llm_model", "-") or "-"
        provider = getattr(self.owner, "llm_provider_name", "-") or "-"
        session = getattr(self.owner, "session_id", "-") or "-"
        return f"  {model}  ·  {provider}  ·  session {session}"

    def _status_line(self) -> str:
        with self.lock:
            status = self.status_text
            started_at = self._started_at
            busy = self.busy
        if busy and started_at is not None:
            return f"{status} · {monotonic() - started_at:.1f}s"
        return status

    def run(self) -> None:
        self.owner.ui = self
        try:
            self.owner._print_startup()
            self.app.run()
        finally:
            self._shutdown_worker()
            self.owner.ui = None

    def _shutdown_worker(self) -> None:
        with self.lock:
            cancel_event = self._active_cancel_event
            thread = self._active_thread
        if cancel_event is not None:
            self._set_cancel_event(cancel_event)
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def write_rich(self, *objects: object, **kwargs: object) -> None:
        buffer = io.StringIO()
        render_console = Console(
            file=buffer,
            width=100,
            color_system=None,
            force_terminal=False,
        )
        render_console.print(*objects, **kwargs)
        text = buffer.getvalue().rstrip()
        if text:
            self.write_plain(text)

    def write_plain(self, text: str) -> None:
        cleaned = text.rstrip()
        if not cleaned:
            return
        with self.lock:
            self.blocks.append(cleaned)
            if len(self.blocks) > self._MAX_BLOCKS:
                self.blocks = self.blocks[-self._MAX_BLOCKS :]
            rendered = "\n\n".join(self.blocks).rstrip() + "\n"
            self.output.text = rendered
            self.output.buffer.cursor_position = len(rendered)
        self.app.invalidate()

    def clear_output(self) -> None:
        with self.lock:
            self.blocks.clear()
            self.output.text = ""
            self.output.buffer.cursor_position = 0
        self.app.invalidate()

    def write_user_message(self, message: str) -> None:
        lines = message.splitlines() or [""]
        body = "\n".join(f"│ {line}" for line in lines)
        self.write_plain(f"╭─ 🧑 你\n{body}\n╰─")

    def is_current_turn(self, generation: int, cancel_event: threading.Event) -> bool:
        with self.lock:
            return (
                self.busy
                and self._generation == generation
                and self._active_cancel_event is cancel_event
            )

    def _handle_interrupt(self, event, *, exit_after: bool) -> None:  # noqa: ANN001
        should_exit = False
        with self.lock:
            cancel_event = self._active_cancel_event
            if not self.busy or cancel_event is None:
                should_exit = True
        if should_exit:
            event.app.exit()
            return

        already_requested = cancel_event.is_set()
        self._set_cancel_event(cancel_event)
        message = "⏹ 已请求取消当前轮；界面保持可用。"
        status = "⏹ 取消中 · 迟到结果会被丢弃"
        with self.lock:
            still_current = self._active_cancel_event is cancel_event and self.busy
            if still_current and (exit_after or already_requested):
                self._exit_requested = True
            if not still_current:
                should_exit = True
            elif self._exit_requested:
                message = "⏹ 已请求取消；当前请求收敛后退出。"
                status = "⏹ 取消中 · 等待当前同步边界收敛后退出"
            elif already_requested:
                message = "⏹ 本轮已在取消中，等待结果收敛…"
                status = "⏹ 取消中 · 迟到结果会被丢弃"
        if should_exit:
            event.app.exit()
            return
        self.write_plain(message)
        self._set_status(status)

    def _set_cancel_event(self, cancel_event: threading.Event) -> None:
        commit_lock = getattr(self.owner, "_commit_lock", None)
        if commit_lock is None:
            cancel_event.set()
            return
        with commit_lock:
            cancel_event.set()

    def _accept(self, buffer) -> bool:  # noqa: ANN001
        message = buffer.text.strip()
        buffer.text = ""
        if not message:
            return True
        with self.lock:
            if self.busy:
                busy = True
            else:
                busy = False
        if busy:
            self.write_plain("⏳ 上一轮仍在处理；按 Ctrl+C 取消，或 Ctrl+Q 取消并退出。")
            return True
        self.write_user_message(message)
        if message.casefold() in {"/exit", "/quit"}:
            self.write_plain("👋 Leon Agent 已退出。")
            self.app.exit()
            return True

        cancel_event = threading.Event()
        with self.lock:
            self._generation += 1
            generation = self._generation
            self.busy = True
            self._active_cancel_event = cancel_event
            self._started_at = monotonic()
            self._exit_requested = False
            thread = threading.Thread(
                target=self._run_message,
                args=(message, generation, cancel_event),
                name=f"leon-turn-{generation}",
                daemon=False,
            )
            self._active_thread = thread
        self._set_status("◐ 处理中 · Ctrl+C 取消当前轮 · Ctrl+Q 取消并退出")
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.busy = False
                self._active_cancel_event = None
                self._active_thread = None
                self._started_at = None
            raise
        return True

    def _run_message(
        self,
        message: str,
        generation: int,
        cancel_event: threading.Event,
    ) -> None:
        token = _ACTIVE_TURN.set((generation, cancel_event))
        try:
            with cancellation_scope(cancel_event):
                keep_running = self.owner.handle_interactive_message(message)
                if not keep_running:
                    with self.lock:
                        self._exit_requested = True
        except AgentCancelled:
            if self.is_current_turn(generation, cancel_event):
                self.write_plain("⏹ 本轮已取消；迟到的模型/工具结果已丢弃。")
        except KeyboardInterrupt:
            if self.is_current_turn(generation, cancel_event):
                self.write_plain("⏹ 本轮已取消；Leon 会话保持不变。")
        except Exception as exc:  # noqa: BLE001 - keep the terminal app alive
            if self.is_current_turn(generation, cancel_event):
                self.write_plain(f"💥 CLI 处理失败：{type(exc).__name__}: {exc}")
        finally:
            _ACTIVE_TURN.reset(token)
            should_exit = False
            current = False
            with self.lock:
                current = (
                    self._generation == generation
                    and self._active_cancel_event is cancel_event
                )
                if current:
                    should_exit = self._exit_requested
                    self.busy = False
                    self._active_cancel_event = None
                    self._active_thread = None
                    self._started_at = None
            if current:
                if should_exit:
                    self._set_status("👋 正在退出…")
                    self.app.exit()
                else:
                    self._set_status(self._IDLE_STATUS)

    def _set_status(self, text: str) -> None:
        self.status_text = text
        self.app.invalidate()


class LeonConsole:
    def __init__(self, args: argparse.Namespace) -> None:
        self.console = Console()
        self.ui: TerminalChatUI | None = None
        config = LeonSettings()
        updates = {}
        if args.backend_url:
            updates["backend_url"] = args.backend_url.rstrip("/")
        if args.public_image_base_url:
            updates["public_image_base_url"] = args.public_image_base_url.rstrip("/")
        if args.plugin_dir:
            updates["plugin_dir"] = Path(args.plugin_dir)
        if args.db:
            updates["session_db"] = Path(args.db)
        self.config = config.model_copy(update=updates)
        self.store = SessionStore(self.config.session_db)
        self.session_id = self._resolve_session(args)
        self.model_selection = self.store.get_model_selection(self.session_id)
        self.model_catalog: list[str] = []
        self.llm_scope = ""
        self.llm_model = ""
        self.llm_profile = ""
        self.llm_provider_name = ""
        self.llm_base_url = ""
        self.llm_source = ""
        self.llm_config_label = ""
        self.llm_timeout_seconds = 0.0
        self.llm_max_retries = 0
        self._progress: Progress | None = None
        self._progress_task_id: int | None = None
        self._commit_lock = threading.Lock()
        self.agent = self._create_agent()

    def _resolve_session(self, args: argparse.Namespace) -> str:
        if args.session and not args.new:
            if not self.store.has_session(args.session):
                raise ValueError(f"Session not found: {args.session}")
            return args.session
        return self.store.create_session()

    def print(self, *objects: object, **kwargs: object) -> None:
        ui = getattr(self, "ui", None)
        if ui is not None:
            ui.write_rich(*objects, **kwargs)
            return
        self.console.print(*objects, **kwargs)

    def _commit_context(self):
        lock = getattr(self, "_commit_lock", None)
        return lock if lock is not None else nullcontext()

    def _check_active_turn(self) -> None:
        cancel_event = current_cancel_event()
        if cancel_event is not None and cancel_event.is_set():
            raise AgentCancelled("agent turn cancelled")
        turn = _ACTIVE_TURN.get()
        ui = getattr(self, "ui", None)
        if turn is not None and ui is not None:
            generation, turn_event = turn
            if not ui.is_current_turn(generation, turn_event):
                raise AgentCancelled("stale agent turn")

    def _create_agent(self) -> LeonAgent:
        reset_settings_cache()
        llm_settings = self._resolve_llm_settings()
        scope = model_provider_scope(
            profile=llm_settings.profile,
            base_url=llm_settings.active_base_url,
        )
        if self.model_selection and self.model_selection[0] != scope:
            self.store.set_model_selection(self.session_id, provider=None, model=None)
            self.model_selection = None
        model_override = self.model_selection[1] if self.model_selection else None
        llm_client = LLMClient(llm_settings, model_override=model_override)
        self.llm_model = llm_client.model
        self.llm_profile = llm_client.profile
        self.llm_provider_name = self._provider_name_from_profile(llm_client.profile)
        self.llm_base_url = llm_settings.active_base_url
        self.llm_source = llm_settings.llm_source
        self.llm_config_label = self._llm_config_label(llm_settings)
        self.llm_timeout_seconds = llm_settings.llm_timeout_seconds
        self.llm_max_retries = llm_settings.llm_max_retries
        self.llm_scope = scope
        self.image_client = LeonImageClient(
            backend_url=self.config.backend_url,
            plugin_dir=self.config.active_plugin_dir,
            public_base_url=self.config.active_public_image_base_url,
            timeout_seconds=self.config.http_timeout_seconds,
            bridge_timeout_seconds=self.config.bridge_timeout_seconds,
        )
        self.direct_tools = create_leon_tools(
            self.image_client,
            session_id=self.session_id,
            default_mode_ids=self.config.default_mode_ids,
        )
        return LeonAgent(
            llm_client=llm_client,
            image_client=self.image_client,
            session_id=self.session_id,
            default_mode_ids=self.config.default_mode_ids,
            on_event=self._on_event,
            additional_system_prompt=self.config.read_additional_system_prompt(),
        )

    @staticmethod
    def _provider_name_from_profile(profile: str) -> str:
        return profile.split(":", 1)[1] if ":" in profile else profile

    @staticmethod
    def _llm_config_label(settings: Settings) -> str:
        if settings.llm_source == "toml":
            return str(settings.codex_config_path)
        if settings.llm_source == "ccs":
            return f"CC Switch / {settings.ccs_app}"
        return ".env / environment"

    def _print_startup(self) -> None:
        title = Text("LEON AGENT", style="bold cyan")
        title.append("  /  terminal cockpit", style="dim")

        body = Text()
        body.append("🧠  Model     ", style="bold cyan")
        body.append(f"{self.llm_model}\n", style="bold")
        body.append("🔌  Provider  ", style="bold cyan")
        body.append(f"{self.llm_provider_name}  ", style="white")
        body.append(f"({self.llm_profile})\n", style="dim")
        body.append("🌐  LLM URL   ", style="bold cyan")
        body.append(f"{self.llm_base_url}\n", style="white")
        body.append("📄  Config    ", style="bold cyan")
        body.append(f"{self.llm_source} · {self.llm_config_label}\n", style="dim")
        body.append("⏱  Request   ", style="bold cyan")
        body.append(
            f"timeout={self.llm_timeout_seconds:g}s · retries={self.llm_max_retries}\n",
            style="dim",
        )
        body.append("🎨  Images    ", style="bold magenta")
        body.append(f"{self.config.backend_url}  ", style="white")
        body.append(f"default={', '.join(self.config.default_mode_ids) or '未配置'}\n", style="dim")
        body.append("🧵  Session   ", style="bold green")
        body.append(f"{self.session_id}\n\n", style="bold")
        body.append("🛑  Cancel    ", style="bold yellow")
        body.append("协作式取消；在途同步请求按超时边界收敛\n", style="dim")
        body.append(
            "✨ /model 选模型    🖼 /nsfw 直达生图    🕹 /history 找会话    ℹ /status 状态\n",
            style="white",
        )
        body.append("🚀 直接输入问题即可聊天；Ctrl+C 取消当前轮，Ctrl+Q 取消并退出", style="dim")

        self.print(
            Panel(
                body,
                title=title,
                subtitle="Enter 发送 · /help 命令 · Ctrl+C 取消 · Ctrl+Q 退出",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    def _resolve_llm_settings(self) -> Settings:
        # LLM base_url/auth always follows the currently active provider in
        # ~/.codex/config.toml (CC Switch writes it). A session model override is
        # passed to LLMClient separately and never changes the provider config.
        return get_settings()

    def _ensure_current_provider(self) -> None:
        reset_settings_cache()
        settings = self._resolve_llm_settings()
        scope = model_provider_scope(profile=settings.profile, base_url=settings.active_base_url)
        if scope != self.llm_scope:
            self.model_catalog = []
            self.agent = self._create_agent()

    def _fetch_model_catalog(self) -> list[str]:
        self._ensure_current_provider()
        settings = self._resolve_llm_settings()
        try:
            models = LLMClient(settings, model_override=self.llm_model).list_models()
        except Exception as exc:  # noqa: BLE001 - manual model entry remains available
            self.print(f"[yellow]模型列表拉取失败：{type(exc).__name__}: {exc}[/yellow]")
            models = []
        self.model_catalog = models
        return models

    def _start_image_progress(self) -> None:
        self._stop_image_progress()
        ui = getattr(self, "ui", None)
        if ui is not None:
            ui.write_plain("🎨 正在生成图片…")
            ui._set_status("Leon 正在等图片任务完成…")
            return
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}"),
            TimeElapsedColumn(),
            console=self.console,
        )
        self._progress.start()
        self._progress_task_id = self._progress.add_task("正在生成图片…", total=None)

    def _stop_image_progress(self, *, ok: bool | None = None) -> None:
        ui = getattr(self, "ui", None)
        if ui is not None:
            if ok is not None:
                ui.write_plain("✅ 图片生成完成" if ok else "❌ 图片生成失败")
            return
        if self._progress is None:
            return
        if self._progress_task_id is not None and ok is not None:
            label = "图片生成完成" if ok else "图片生成失败"
            self._progress.update(self._progress_task_id, description=label)
        self._progress.stop()
        self._progress = None
        self._progress_task_id = None

    def _on_event(self, event: AgentEvent) -> None:
        turn = _ACTIVE_TURN.get()
        ui = getattr(self, "ui", None)
        if turn is not None and ui is not None:
            generation, cancel_event = turn
            if not ui.is_current_turn(generation, cancel_event):
                return
            if cancel_event.is_set() and event.kind != "cancelled":
                return
        if event.kind == "turn_started":
            if ui is not None:
                ui._set_status(f"◐ 模型思考中 · 第 {event.turn} 轮 · Ctrl+C 取消")
            return
        if event.kind == "cancelled":
            if ui is not None:
                ui._set_status("⏹ 取消中 · 迟到结果会被丢弃")
            return
        if event.kind == "tool_started":
            if event.tool_name == "generate_images":
                self._start_image_progress()
                return
            self.print(
                f"[cyan]●[/cyan] [bold]调用工具[/bold] [cyan]{event.tool_name}[/cyan]"
            )
        elif event.kind == "tool_finished":
            ok = bool(event.result and event.result.get("ok"))
            if event.tool_name == "generate_images":
                self._stop_image_progress(ok=ok)
            if ok:
                self.print(f"[green]✓[/green] [dim]{event.tool_name} 完成[/dim]")
            else:
                self.print(f"[red]✗[/red] [dim]{event.tool_name} 失败[/dim]")

    def _print_answer(self, answer: str) -> None:
        self.print(
            Panel(
                Markdown(answer),
                title="🤖 Leon",
                border_style="green",
                padding=(0, 1),
            )
        )

    def _start_llm_request(self) -> None:
        """Show feedback before the provider call, including in the legacy REPL."""
        self._check_active_turn()
        model = self.llm_model or "当前模型"
        self.print(f"[cyan]⏳[/cyan] 正在请求模型 [bold]{model}[/bold]…")
        ui = getattr(self, "ui", None)
        if ui is not None:
            ui._set_status(f"正在请求模型 {model}…")

    def _format_request_error(self, exc: Exception) -> str:
        error_type = type(exc).__name__
        detail = str(exc).strip()
        if error_type in {"APITimeoutError", "TimeoutError", "ReadTimeout"}:
            timeout = getattr(self, "llm_timeout_seconds", 30.0)
            retries = getattr(self, "llm_max_retries", 0)
            return (
                f"模型请求超时（{timeout:g}s，自动重试 {retries} 次），"
                "请检查 provider、模型 ID 或网络后重试。"
            )
        if error_type in {"APIConnectionError", "ConnectError", "ReadError"}:
            return "模型 provider 连接失败，请检查 base URL/网络后重试。"
        suffix = f": {detail}" if detail else ""
        return f"请求失败：{error_type}{suffix}"

    def process(self, message: str) -> bool:
        stripped = message.strip()
        try:
            self._check_active_turn()
            if stripped.casefold() == "/nsfw" or stripped.casefold().startswith("/nsfw "):
                return self._process_nsfw(stripped)
            self._ensure_current_provider()
            self._check_active_turn()
            history = self.store.load_messages(self.session_id)
            self._start_llm_request()
            result = self.agent.run(message, history=history)
            self._check_active_turn()
        except KeyboardInterrupt:
            self._stop_image_progress(ok=False)
            self.print("[yellow]⚠ 本次请求已取消，Leon 仍在运行。[/yellow]")
            return False
        except AgentCancelled:
            self._stop_image_progress(ok=None)
            self.print("[yellow]⏹ 本次请求已取消，迟到结果已丢弃。[/yellow]")
            return False
        except Exception as exc:  # noqa: BLE001 - CLI should keep the session alive
            self._stop_image_progress(ok=False)
            self.print(f"[red]{self._format_request_error(exc)}[/red]")
            return False
        with self._commit_context():
            self._check_active_turn()
            self.store.add_message(self.session_id, "user", message)
            self.store.record_result(self.session_id, result)
            self.store.add_message(self.session_id, "assistant", result.answer)
        self._check_active_turn()
        self._print_answer(result.answer)
        return True

    def _process_nsfw(self, message: str) -> bool:
        try:
            self._check_active_turn()
            mode_result = self.image_client.list_modes()
            self._check_active_turn()
            modes = mode_result.get("modes", [])
            command = parse_nsfw_command(message, modes)
        except AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - invalid command should not exit the REPL
            self.print(f"[red]{exc}[/red]")
            if "modes" in locals():
                self.print(Markdown(format_mode_catalog(modes)))
            return False
        if command is None:
            self.print(Markdown(format_mode_catalog(modes)))
            return True
        arguments = {
            "source_text": command.source_text,
            "workflow_ids": [command.workflow_id],
            "batch_count": 1,
        }
        self._start_image_progress()
        try:
            self._check_active_turn()
            result = self.direct_tools.execute("generate_images", arguments)
            self._check_active_turn()
        except KeyboardInterrupt:
            self._stop_image_progress(ok=False)
            self.print("[yellow]⚠ 本次生图已取消，Leon 仍在运行。[/yellow]")
            return False
        except AgentCancelled:
            self._stop_image_progress(ok=None)
            raise
        except Exception as exc:  # noqa: BLE001 - image failure should not exit the REPL
            self._stop_image_progress(ok=False)
            self.print(f"[red]直达生图失败：{type(exc).__name__}: {exc}[/red]")
            return False
        ok = bool(result.get("ok"))
        self._stop_image_progress(ok=ok)
        if not ok:
            self.print(f"[red]直达生图失败：{result.get('error') or '未知错误'}[/red]")
            return False
        images = [
            item.get("image_url")
            for item in result.get("images", [])
            if isinstance(item, dict) and item.get("image_url")
        ]
        answer = f"{command.mode_name}模式的图片生成好了。"
        if images:
            answer += "\n\n" + "\n".join(f"- {url}" for url in images)
        agent_result = AgentResult(
            answer=answer,
            steps=[ToolStep("generate_images", arguments, result)],
        )
        with self._commit_context():
            self._check_active_turn()
            self.store.add_message(self.session_id, "user", message)
            self.store.record_result(self.session_id, agent_result)
            self.store.add_message(self.session_id, "assistant", answer)
        self._check_active_turn()
        self._print_answer(answer)
        return True

    def show_history(self) -> None:
        table = Table("Session", "Messages", "Updated")
        for item in self.store.list_sessions():
            table.add_row(
                item["id"],
                str(item["message_count"]),
                str(item["updated_at"]),
            )
        self.print(table)

    def show_status(self) -> None:
        model = getattr(self, "llm_model", "-") or "-"
        provider = getattr(self, "llm_provider_name", "") or getattr(
            self, "llm_profile", "-"
        )
        body = Text()
        body.append("模型       ", style="bold cyan")
        body.append(f"{model}\n")
        body.append("Provider   ", style="bold cyan")
        body.append(f"{provider or '-'}\n")
        body.append("会话       ", style="bold cyan")
        body.append(f"{self.session_id}\n")
        body.append("请求策略   ", style="bold cyan")
        body.append(
            f"timeout={getattr(self, 'llm_timeout_seconds', 30):g}s · "
            f"retries={getattr(self, 'llm_max_retries', 0)}\n"
        )
        body.append("图片后端   ", style="bold magenta")
        body.append(f"{getattr(getattr(self, 'config', None), 'backend_url', '-')}")
        self.print(Panel(body, title="当前运行状态", border_style="cyan"))

    def show_models(self) -> None:
        models = self._fetch_model_catalog()
        self.print(
            f"当前模型：[bold]{self.llm_model}[/bold]  provider={self.llm_profile}"
        )
        table = Table("#", "Model", "Current")
        for index, model_id in enumerate(models, start=1):
            table.add_row(str(index), model_id, "*" if model_id == self.llm_model else "")
        if self.llm_model not in models:
            table.add_row("自定义", self.llm_model, "*")
        self.print(table)
        if not models:
            self.print("[dim]供应商未返回模型列表，仍可直接输入完整模型 ID。[/dim]")
        self.print("使用 /model <序号或模型ID> 切换，/model default 恢复默认。")

    def switch_model(self, value: str) -> None:
        candidate = value.strip()
        if candidate.casefold() == "default":
            self.store.set_model_selection(
                self.session_id,
                provider=None,
                model=None,
            )
            self.model_selection = None
            self.agent = self._create_agent()
            self.print(
                f"[green]已恢复默认模型[/green] {self.llm_model} ({self.llm_profile})"
            )
            return

        # The cached numeric catalog belongs to one provider scope. Refresh the
        # scope before resolving a numeric shortcut so a CC Switch change cannot
        # map an index from the previous provider into the new session selection.
        self._ensure_current_provider()
        catalog = self.model_catalog
        if candidate.isdigit() and not catalog:
            catalog = self._fetch_model_catalog()
        model_id = resolve_model_id(candidate, catalog)
        if model_id is None:
            self.print(f"[red]未知模型：{candidate}[/red]")
            self.show_models()
            return

        settings = self._resolve_llm_settings()
        scope = model_provider_scope(profile=settings.profile, base_url=settings.active_base_url)
        self.store.set_model_selection(
            self.session_id,
            provider=scope,
            model=model_id,
        )
        self.model_selection = (scope, model_id)
        self.agent = self._create_agent()
        self.print(f"[green]已切换模型[/green] {self.llm_model} ({self.llm_profile})")

    def new_session(self) -> None:
        self.session_id = self.store.create_session()
        self.model_selection = None
        self.agent = self._create_agent()
        self.print(f"[green]✓[/green] 新会话 [bold]{self.session_id}[/bold]")

    def handle_interactive_message(self, message: str) -> bool:
        """Handle one command or chat turn for either terminal frontend."""
        message = message.strip()
        if not message:
            return True
        self._check_active_turn()
        if message in {"/exit", "/quit"}:
            self.print("[dim]Leon Agent 已退出。[/dim]")
            return False
        if message == "/new":
            self.new_session()
            self._check_active_turn()
            return True
        if message == "/history":
            self.show_history()
            self._check_active_turn()
            return True
        if message == "/status":
            self.show_status()
            self._check_active_turn()
            return True
        if message == "/clear":
            ui = getattr(self, "ui", None)
            if ui is not None:
                ui.clear_output()
                ui._set_status(TerminalChatUI._IDLE_STATUS)
            else:
                self.console.clear()
            self._check_active_turn()
            return True
        if message == "/model":
            self.show_models()
            self._check_active_turn()
            return True
        if message.startswith("/model "):
            self.switch_model(message.removeprefix("/model "))
            self._check_active_turn()
            return True
        if message == "/help":
            self.print(
                Panel(
                    "[bold]/new[/bold] 新会话\n"
                    "[bold]/history[/bold] 会话列表\n"
                    "[bold]/status[/bold] 当前模型、provider、会话状态\n"
                    "[bold]/model[/bold] 查看模型\n"
                    "[bold]/model <序号或模型ID>[/bold] 切换模型\n"
                    "[bold]/clear[/bold] 清空当前终端滚动区\n"
                    "[bold]/nsfw <描述>[/bold] 跳过 LLM，直接用 NSFW 模式生图\n"
                    "[bold]/exit[/bold] 退出\n\n"
                    "[dim]快捷键：Ctrl+C 取消当前轮 · Ctrl+Q 取消并退出 · Ctrl+L 清屏[/dim]\n\n"
                    "[dim]你也可以直接说：检查环境、生成图片、查询任务、查看最近图片。[/dim]",
                    title="Leon 命令",
                    border_style="dim",
                )
            )
            self._check_active_turn()
            return True
        if message.casefold() == "/nsfw" or message.casefold().startswith("/nsfw "):
            self.process(message)
            return True
        if message.startswith("/"):
            self.print(
                f"[yellow]未知命令：{message.split(maxsplit=1)[0]}[/yellow] · "
                "输入 /help 查看可用命令"
            )
            return True
        self.process(message)
        return True

    def interactive(self) -> None:
        if TerminalChatUI.available() and sys.stdin.isatty() and sys.stdout.isatty():
            TerminalChatUI(self).run()
            return
        self._legacy_interactive()

    def _legacy_interactive(self) -> None:
        self._print_startup()
        while True:
            try:
                message = Prompt.ask("\n[bold cyan]你[/bold cyan]").strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]Leon Agent 已退出。[/dim]")
                return
            if not message:
                continue
            if not self.handle_interactive_message(message):
                return


def main() -> None:
    args = parse_args()
    try:
        app = LeonConsole(args)
    except Exception as exc:  # noqa: BLE001 - provide a readable startup failure
        Console().print(f"[red]Leon Agent 启动失败：{type(exc).__name__}: {exc}[/red]")
        raise SystemExit(1) from exc
    if args.once:
        raise SystemExit(0 if app.process(args.once) else 1)
    app.interactive()


if __name__ == "__main__":
    main()
