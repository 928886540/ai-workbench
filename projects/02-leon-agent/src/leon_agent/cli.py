"""Interactive `leon` command."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from workbench_core.agent import AgentEvent
from workbench_core.config import Settings, get_settings, reset_settings_cache
from workbench_core.llm import LLMClient

from leon_agent.agent import LeonAgent
from leon_agent.config import LeonSettings
from leon_agent.leon_client import LeonImageClient
from leon_agent.models import MODEL_IDS, resolve_model_id
from leon_agent.session import SessionStore


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


class LeonConsole:
    def __init__(self, args: argparse.Namespace) -> None:
        self.console = Console()
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
        self.agent = self._create_agent()

    def _resolve_session(self, args: argparse.Namespace) -> str:
        if args.session and not args.new:
            if not self.store.has_session(args.session):
                raise ValueError(f"Session not found: {args.session}")
            return args.session
        return self.store.create_session()

    def _create_agent(self) -> LeonAgent:
        reset_settings_cache()
        llm_settings = self._resolve_llm_settings()
        model_override = self.model_selection[1] if self.model_selection else None
        llm_client = LLMClient(llm_settings, model_override=model_override)
        self.llm_model = llm_client.model
        self.llm_profile = llm_client.profile
        image_client = LeonImageClient(
            backend_url=self.config.backend_url,
            plugin_dir=self.config.active_plugin_dir,
            public_base_url=self.config.active_public_image_base_url,
            timeout_seconds=self.config.http_timeout_seconds,
            bridge_timeout_seconds=self.config.bridge_timeout_seconds,
        )
        return LeonAgent(
            llm_client=llm_client,
            image_client=image_client,
            session_id=self.session_id,
            default_mode_ids=self.config.default_mode_ids,
            on_event=self._on_event,
        )

    def _print_startup(self) -> None:
        title = Text("LEON AGENT", style="bold cyan")
        title.append("  /  interactive runtime", style="dim")

        body = Text()
        body.append("会话  ", style="dim")
        body.append(f"{self.session_id}\n", style="bold")
        body.append("后端  ", style="dim")
        body.append(f"{self.config.backend_url}\n")
        body.append("模型  ", style="dim")
        body.append(f"{self.llm_model} ({self.llm_profile})\n")
        body.append("生图  ", style="dim")
        body.append(", ".join(self.config.default_mode_ids) or "未配置", style="green")
        body.append("\n\n")
        body.append("直接聊天，或用自然语言让 Agent 调用工具。\n", style="white")
        body.append("例如：", style="dim")
        body.append("“检查生图环境，然后生成一张雨夜东京街景”\n", style="italic")
        body.append("提示：", style="yellow")
        body.append(" 生图任务可能需要一些时间；Agent 会显示工具调用和任务状态。", style="dim")

        self.console.print(
            Panel(
                body,
                title=title,
                subtitle="/help 命令  ·  /new 新会话  ·  /model 模型  ·  /exit 退出",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    def _resolve_llm_settings(self) -> Settings:
        # LLM base_url/auth always follows the currently active provider in
        # ~/.codex/config.toml (CC Switch writes it). A session model override is
        # passed to LLMClient separately and never changes the provider config.
        return get_settings()

    def _on_event(self, event: AgentEvent) -> None:
        if event.kind == "tool_started":
            self.console.print(
                f"[cyan]●[/cyan] [bold]调用工具[/bold] [cyan]{event.tool_name}[/cyan]"
            )
        elif event.kind == "tool_finished":
            ok = bool(event.result and event.result.get("ok"))
            if ok:
                self.console.print(f"[green]✓[/green] [dim]{event.tool_name} 完成[/dim]")
            else:
                self.console.print(f"[red]✗[/red] [dim]{event.tool_name} 失败[/dim]")

    def process(self, message: str) -> bool:
        history = self.store.load_messages(self.session_id)
        self.store.add_message(self.session_id, "user", message)
        try:
            result = self.agent.run(message, history=history)
        except Exception as exc:  # noqa: BLE001 - CLI should keep the session alive
            error = f"请求失败：{type(exc).__name__}: {exc}"
            self.store.add_message(self.session_id, "assistant", error)
            self.console.print(f"[red]{error}[/red]")
            return False
        self.store.record_result(self.session_id, result)
        self.store.add_message(self.session_id, "assistant", result.answer)
        self.console.print(Markdown(result.answer))
        return True

    def show_history(self) -> None:
        table = Table("Session", "Messages", "Updated")
        for item in self.store.list_sessions():
            table.add_row(
                item["id"],
                str(item["message_count"]),
                str(item["updated_at"]),
            )
        self.console.print(table)

    def show_models(self) -> None:
        self.console.print(
            f"当前模型：[bold]{self.llm_model}[/bold]  provider={self.llm_profile}"
        )
        table = Table("#", "Model", "Current")
        for index, model_id in enumerate(MODEL_IDS, start=1):
            table.add_row(str(index), model_id, "*" if model_id == self.llm_model else "")
        self.console.print(table)
        self.console.print("使用 /model <序号或模型ID> 切换，/model default 恢复默认。")

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
            self.console.print(
                f"[green]已恢复默认模型[/green] {self.llm_model} ({self.llm_profile})"
            )
            return

        model_id = resolve_model_id(candidate)
        if model_id is None:
            self.console.print(f"[red]未知模型：{candidate}[/red]")
            self.show_models()
            return

        settings = self._resolve_llm_settings()
        self.store.set_model_selection(
            self.session_id,
            provider=settings.profile,
            model=model_id,
        )
        self.model_selection = (settings.profile, model_id)
        self.agent = self._create_agent()
        self.console.print(f"[green]已切换模型[/green] {self.llm_model} ({self.llm_profile})")

    def new_session(self) -> None:
        self.session_id = self.store.create_session()
        self.model_selection = None
        self.agent = self._create_agent()
        self.console.print(f"[green]✓[/green] 新会话 [bold]{self.session_id}[/bold]")

    def interactive(self) -> None:
        self._print_startup()
        while True:
            try:
                message = Prompt.ask("\n[bold cyan]你[/bold cyan]").strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]Leon Agent 已退出。[/dim]")
                return
            if not message:
                continue
            if message in {"/exit", "/quit"}:
                self.console.print("[dim]Leon Agent 已退出。[/dim]")
                return
            if message == "/new":
                self.new_session()
                continue
            if message == "/history":
                self.show_history()
                continue
            if message == "/model":
                self.show_models()
                continue
            if message.startswith("/model "):
                self.switch_model(message.removeprefix("/model "))
                continue
            if message == "/help":
                self.console.print(
                    Panel(
                        "[bold]/new[/bold] 新会话\n"
                        "[bold]/history[/bold] 会话列表\n"
                        "[bold]/model[/bold] 查看模型\n"
                        "[bold]/model <序号或模型ID>[/bold] 切换模型\n"
                        "[bold]/exit[/bold] 退出\n\n"
                        "[dim]你也可以直接说：检查环境、生成图片、查询任务、查看最近图片。[/dim]",
                        title="Leon 命令",
                        border_style="dim",
                    )
                )
                continue
            self.process(message)


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
