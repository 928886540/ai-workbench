"""Interactive `leon` command."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from workbench_core.agent import AgentEvent
from workbench_core.config import get_settings, reset_settings_cache
from workbench_core.llm import LLMClient

from leon_agent.agent import LeonAgent
from leon_agent.config import LeonSettings
from leon_agent.leon_client import LeonImageClient
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
        updates: dict[str, object] = {}
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

        # streaming / progress state
        self._streaming = False
        self._progress: Progress | None = None
        self._image_task_ids: dict[str, object] = {}
        self._pending_image_jobs: set[str] = set()
        self._completed_image_urls: dict[str, str] = {}

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

    def _print_startup(self) -> None:
        title = Text("LEON AGENT", style="bold cyan")
        title.append("  /  interactive runtime", style="dim")

        body = Text()
        body.append("会话  ", style="dim")
        body.append(f"{self.session_id}\n", style="bold")
        body.append("后端  ", style="dim")
        body.append(f"{self.config.backend_url}\n")
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
                subtitle="/help 命令  ·  /new 新会话  ·  /history 历史  ·  /exit 退出",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    def _on_event(self, event: AgentEvent) -> None:  # noqa: C901
        kind = event.kind

        # 7-B: streaming delta
        if kind == "assistant_delta":
            delta = (event.result or {}).get("delta", "")
            if delta:
                if not self._streaming:
                    sys.stdout.write("\n\033[1;32mLeon\033[0m  ")
                    self._streaming = True
                sys.stdout.write(delta)
                sys.stdout.flush()
            return

        if kind == "assistant_completed":
            if self._streaming:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._streaming = False
            return

        if kind == "tool_started":
            self.console.print(f"[cyan]●[/cyan] [bold]调用工具[/bold] [cyan]{event.tool_name}[/cyan]")

        elif kind == "tool_finished":
            ok = bool(event.result and event.result.get("ok"))
            if ok:
                self.console.print(f"[green]✓[/green] [dim]{event.tool_name} 完成[/dim]")
            else:
                self.console.print(f"[red]✗[/red] [dim]{event.tool_name} 失败[/dim]")

        # 7-C: image progress bar
        elif kind == "image_task_created":
            job_id = str((event.result or {}).get("job_id", "?"))
            self._pending_image_jobs.add(job_id)
            if self._progress is None:
                self._progress = Progress(
                    SpinnerColumn(),
                    TextColumn("[bold cyan]{task.description}[/bold cyan]"),
                    BarColumn(bar_width=24),
                    TextColumn("[dim]{task.fields[status]}[/dim]"),
                    TimeElapsedColumn(),
                    console=self.console,
                    transient=False,
                )
                self._progress.start()
            rich_task_id = self._progress.add_task(
                f"🎨 [{job_id[:8]}]",
                total=100,
                status="queued",
            )
            self._image_task_ids[job_id] = rich_task_id

        elif kind == "image_task_updated":
            job_id = str((event.result or {}).get("job_id", ""))
            status = str((event.result or {}).get("status", "running"))
            if self._progress and job_id in self._image_task_ids:
                advance = {"queued": 0, "running": 30, "processing": 60}.get(status, 0)
                self._progress.update(
                    self._image_task_ids[job_id],  # type: ignore[arg-type]
                    completed=advance,
                    status=status,
                )

        elif kind == "image_completed":
            job_id = str((event.result or {}).get("job_id", ""))
            url = str((event.result or {}).get("image_url", ""))
            self._completed_image_urls[job_id] = url
            if self._progress and job_id in self._image_task_ids:
                self._progress.update(
                    self._image_task_ids[job_id],  # type: ignore[arg-type]
                    completed=100,
                    status="[green]done ✓[/green]",
                )
            self._pending_image_jobs.discard(job_id)

    def _stop_progress(self) -> None:
        if self._progress:
            self._progress.stop()
            self._progress = None
            self._image_task_ids.clear()

    def _show_image_results(self) -> None:
        """Print clickable URLs for each completed image."""
        for url in self._completed_image_urls.values():
            # Rich hyperlink: terminals that support OSC 8 make this clickable
            self.console.print(
                f"\n[bold green]✔ 图片就绪[/bold green]  [link={url}]{url}[/link]"
            )
        self._completed_image_urls.clear()
        self._pending_image_jobs.clear()

    def process(self, message: str) -> bool:
        history = self.store.load_messages(self.session_id)
        self.store.add_message(self.session_id, "user", message)
        self._streaming = False
        try:
            result = self.agent.run(message, history=history)
        except Exception as exc:  # noqa: BLE001
            self._stop_progress()
            if self._streaming:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._streaming = False
            error = f"请求失败：{type(exc).__name__}: {exc}"
            self.store.add_message(self.session_id, "assistant", error)
            self.console.print(f"[red]{error}[/red]")
            return False
        finally:
            if self._streaming:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._streaming = False

        self._stop_progress()
        self.store.record_result(self.session_id, result)
        self.store.add_message(self.session_id, "assistant", result.answer)

        # If no streaming delta events came through, print full answer now
        if result.answer:
            self.console.print(Markdown(result.answer))

        self._show_image_results()
        return True

    def show_history(self) -> None:
        table = Table("会话", "消息数", "更新时间")
        for item in self.store.list_sessions():
            table.add_row(item["id"], str(item["message_count"]), str(item["updated_at"]))
        self.console.print(table)

    def new_session(self) -> None:
        self.session_id = self.store.create_session()
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
            if message == "/help":
                self.console.print(
                    Panel(
                        "[bold]/new[/bold] 新会话\n"
                        "[bold]/history[/bold] 会话列表\n"
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
    except Exception as exc:  # noqa: BLE001
        Console().print(f"[red]Leon Agent 启动失败：{type(exc).__name__}: {exc}[/red]")
        raise SystemExit(1) from exc
    if args.once:
        raise SystemExit(0 if app.process(args.once) else 1)
    app.interactive()


if __name__ == "__main__":
    main()
