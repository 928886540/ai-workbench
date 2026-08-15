"""Interactive `leon` command."""

from __future__ import annotations

import argparse
import io
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from workbench_core.agent import AgentEvent, AgentResult, ToolStep
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
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.widgets import Frame, Label, TextArea
except ModuleNotFoundError:  # pragma: no cover - legacy prompt fallback remains usable
    Application = None
    KeyBindings = None
    HSplit = None
    Layout = None
    Dimension = None
    Frame = None
    Label = None
    TextArea = None


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
    """Fullscreen chat surface: scrollback above, input pinned at the bottom."""

    _MAX_BLOCKS = 240

    def __init__(self, owner: LeonConsole) -> None:
        if Application is None:
            raise RuntimeError("prompt_toolkit is not installed")
        self.owner = owner
        self.blocks: list[str] = []
        self.lock = threading.Lock()
        self.busy = False
        self.status_text = "Enter 发送 · /help 命令 · Ctrl+Q 退出"

        self.output = TextArea(
            text="",
            read_only=True,
            scrollbar=True,
            wrap_lines=True,
        )
        self.input = TextArea(
            height=1,
            prompt="你 > ",
            multiline=False,
            accept_handler=self._accept,
        )
        self.status = Label(lambda: self.status_text)
        key_bindings = KeyBindings()

        @key_bindings.add("c-q")
        @key_bindings.add("c-c")
        def _(event) -> None:  # noqa: ANN001
            event.app.exit()

        root = HSplit(
            [
                Frame(
                    self.output,
                    title="💬 LEON AGENT · 上方滚动区",
                    height=Dimension(weight=1),
                ),
                self.status,
                Frame(self.input, title="⌨ 底部输入框 · Enter 发送"),
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

    def run(self) -> None:
        self.owner.ui = self
        try:
            self.owner._print_startup()
            self.app.run()
        finally:
            self.owner.ui = None

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

    def write_user_message(self, message: str) -> None:
        lines = message.splitlines() or [""]
        body = "\n".join(f"│ {line}" for line in lines)
        self.write_plain(f"╭─ 🧑 你\n{body}\n╰─")

    def _accept(self, buffer) -> bool:  # noqa: ANN001
        message = buffer.text.strip()
        buffer.text = ""
        if not message:
            return True
        if self.busy:
            self.write_plain("⏳ 上一条还在处理，等 Leon 回完再发下一条。")
            return True
        self.write_user_message(message)
        if message in {"/exit", "/quit"}:
            self.write_plain("👋 Leon Agent 已退出。")
            self.app.exit()
            return True
        self.busy = True
        self._set_status("Leon 正在处理这一轮…")
        thread = threading.Thread(
            target=self._run_message,
            args=(message,),
            daemon=True,
        )
        thread.start()
        return True

    def _run_message(self, message: str) -> None:
        try:
            keep_running = self.owner.handle_interactive_message(message)
            if not keep_running:
                self.app.exit()
        except Exception as exc:  # noqa: BLE001 - keep the terminal app alive
            self.write_plain(f"💥 CLI 处理失败：{type(exc).__name__}: {exc}")
        finally:
            self.busy = False
            self._set_status("Enter 发送 · /help 命令 · Ctrl+Q 退出")

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
        self._progress: Progress | None = None
        self._progress_task_id: int | None = None
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
        body.append("🎨  Images    ", style="bold magenta")
        body.append(f"{self.config.backend_url}  ", style="white")
        body.append(f"default={', '.join(self.config.default_mode_ids) or '未配置'}\n", style="dim")
        body.append("🧵  Session   ", style="bold green")
        body.append(f"{self.session_id}\n\n", style="bold")
        body.append("✨ /model 选模型    🖼 /nsfw 直达生图    🕹 /history 找会话\n", style="white")
        body.append("🚀 也可以直接说：检查环境、生成图片、查看最近 5 张图", style="dim")

        self.print(
            Panel(
                body,
                title=title,
                subtitle="底部输入 · Enter 发送 · /help 查看命令 · /exit 退出",
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

    def process(self, message: str) -> bool:
        stripped = message.strip()
        if stripped.casefold() == "/nsfw" or stripped.casefold().startswith("/nsfw "):
            return self._process_nsfw(stripped)
        self._ensure_current_provider()
        history = self.store.load_messages(self.session_id)
        try:
            result = self.agent.run(message, history=history)
        except Exception as exc:  # noqa: BLE001 - CLI should keep the session alive
            self._stop_image_progress(ok=False)
            error = f"请求失败：{type(exc).__name__}: {exc}"
            self.print(f"[red]{error}[/red]")
            return False
        self.store.add_message(self.session_id, "user", message)
        self.store.record_result(self.session_id, result)
        self.store.add_message(self.session_id, "assistant", result.answer)
        self._print_answer(result.answer)
        return True

    def _process_nsfw(self, message: str) -> bool:
        try:
            mode_result = self.image_client.list_modes()
            modes = mode_result.get("modes", [])
            command = parse_nsfw_command(message, modes)
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
            result = self.direct_tools.execute("generate_images", arguments)
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
        self.store.add_message(self.session_id, "user", message)
        self.store.record_result(self.session_id, agent_result)
        self.store.add_message(self.session_id, "assistant", answer)
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

        catalog = self.model_catalog
        if candidate.isdigit() and not catalog:
            catalog = self._fetch_model_catalog()
        model_id = resolve_model_id(candidate, catalog)
        if model_id is None:
            self.print(f"[red]未知模型：{candidate}[/red]")
            self.show_models()
            return

        self._ensure_current_provider()
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
        if message in {"/exit", "/quit"}:
            self.print("[dim]Leon Agent 已退出。[/dim]")
            return False
        if message == "/new":
            self.new_session()
            return True
        if message == "/history":
            self.show_history()
            return True
        if message == "/model":
            self.show_models()
            return True
        if message.startswith("/model "):
            self.switch_model(message.removeprefix("/model "))
            return True
        if message == "/help":
            self.print(
                Panel(
                    "[bold]/new[/bold] 新会话\n"
                    "[bold]/history[/bold] 会话列表\n"
                    "[bold]/model[/bold] 查看模型\n"
                    "[bold]/model <序号或模型ID>[/bold] 切换模型\n"
                    "[bold]/nsfw <描述>[/bold] 跳过 LLM，直接用 NSFW 模式生图\n"
                    "[bold]/exit[/bold] 退出\n\n"
                    "[dim]你也可以直接说：检查环境、生成图片、查询任务、查看最近图片。[/dim]",
                    title="Leon 命令",
                    border_style="dim",
                )
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
