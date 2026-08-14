"""Interactive `leon` command."""

from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.table import Table
from workbench_core.agent import AgentEvent
from workbench_core.config import get_settings, reset_settings_cache
from workbench_core.llm import LLMClient

from leon_agent.agent import LeonAgent
from leon_agent.config import LeonSettings
from leon_agent.leon_client import LeonImageClient
from leon_agent.session import SessionStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chat with Leon Agent and use Leon image tools")
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
        self.agent = self._create_agent()

    def _resolve_session(self, args: argparse.Namespace) -> str:
        if args.session and not args.new:
            if not self.store.has_session(args.session):
                raise ValueError(f"Session not found: {args.session}")
            return args.session
        return self.store.create_session()

    def _create_agent(self) -> LeonAgent:
        reset_settings_cache()
        llm_client = LLMClient(get_settings())
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

    def _on_event(self, event: AgentEvent) -> None:
        if event.kind == "tool_started":
            self.console.print(f"[cyan]调用工具[/cyan] {event.tool_name}")
        elif event.kind == "tool_finished":
            ok = bool(event.result and event.result.get("ok"))
            state = "完成" if ok else "失败"
            self.console.print(f"[dim]{event.tool_name}: {state}[/dim]")

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

    def new_session(self) -> None:
        self.session_id = self.store.create_session()
        self.agent = self._create_agent()
        self.console.print(f"[green]新会话[/green] {self.session_id}")

    def interactive(self) -> None:
        self.console.print("[bold]Leon Agent[/bold]")
        self.console.print(f"session={self.session_id}")
        self.console.print("输入 /help 查看命令，/exit 退出。")
        while True:
            try:
                message = Prompt.ask("\n[bold cyan]你[/bold cyan]").strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n已退出。")
                return
            if not message:
                continue
            if message in {"/exit", "/quit"}:
                return
            if message == "/new":
                self.new_session()
                continue
            if message == "/history":
                self.show_history()
                continue
            if message == "/help":
                self.console.print("/new 新会话  /history 会话列表  /exit 退出")
                continue
            self.process(message)


def main() -> None:
    args = build_parser().parse_args()
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
