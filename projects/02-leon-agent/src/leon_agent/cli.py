"""Interactive `leon` command."""

from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Sequence
from contextlib import nullcontext
from contextvars import ContextVar
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, unquote, urlsplit

from rich.console import Console, Group
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
from leon_agent.config_file import apply_config_file
from leon_agent.file_tools import create_file_search_service
from leon_agent.file_write_policy import create_file_write_service
from leon_agent.image_modes import format_mode_catalog, parse_nsfw_command
from leon_agent.leon_client import LeonImageClient
from leon_agent.memory.service import MemoryService
from leon_agent.memory.store import MemoryStore
from leon_agent.models import model_provider_scope, resolve_model_id
from leon_agent.search import create_search_service
from leon_agent.session import SessionStore
from leon_agent.tools import create_leon_tools

if sys.platform == "win32":
    try:
        from prompt_toolkit.input.defaults import create_input
        from prompt_toolkit.input.win32 import ConsoleInputReader, Win32Input
        from prompt_toolkit.key_binding.key_processor import KeyPress
        from prompt_toolkit.keys import Keys
    except ModuleNotFoundError:  # pragma: no cover - optional TUI dependency
        create_input = None
        ConsoleInputReader = None
        Win32Input = None
        KeyPress = None
        Keys = None
else:  # pragma: no cover - platform-specific compatibility shim
    create_input = None
    ConsoleInputReader = None
    Win32Input = None
    KeyPress = None
    Keys = None

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.filters import Condition, has_focus
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.layout.margins import ScrollbarMargin
    from prompt_toolkit.mouse_events import MouseButton, MouseEventType
    from prompt_toolkit.styles import Style
    from prompt_toolkit.widgets import TextArea
except ModuleNotFoundError:  # pragma: no cover - legacy prompt fallback remains usable
    Application = None
    WordCompleter = None
    Point = None
    Condition = None
    has_focus = None
    KeyBindings = None
    HSplit = None
    Layout = None
    Window = None
    FormattedTextControl = None
    Dimension = None
    InMemoryHistory = None
    ScrollbarMargin = None
    MouseButton = None
    MouseEventType = None
    Style = None
    TextArea = None


_ACTIVE_TURN: ContextVar[tuple[int, threading.Event] | None] = ContextVar(
    "leon_cli_active_turn",
    default=None,
)

_CLI_COMMAND_META = {
    "/help": "查看命令和快捷键",
    "/commands": "查看全部命令",
    "/new": "创建新会话",
    "/history": "列出最近会话",
    "/resume": "切换已有会话",
    "/retry": "重试上一条请求",
    "/last": "查看上一条回答",
    "/copy": "复制上一条回答",
    "/open": "打开最近图片",
    "/tools": "查看已注册工具",
    "/status": "查看模型与运行状态",
    "/info": "查看模型与运行状态",
    "/model": "查看或切换模型",
    "/models": "打开模型选择器",
    "/clear": "清空终端滚动区",
    "/nsfw": "跳过 LLM 直达生图",
    "/exit": "退出 Leon",
    "/quit": "退出 Leon",
}
_CLI_COMMANDS = list(_CLI_COMMAND_META)

_NEWLINE_ENTER_DATA = {
    "\x1b[27;2;13~",  # xterm modifyOtherKeys
    "\x1b[27;5;13~",
    "\x1b[13;2u",  # Kitty/CSI-u keyboard protocol
    "\x1b[13;5u",
}

_WIN32_SHIFT_PRESSED = 0x0010
_WIN32_CTRL_PRESSED = 0x000C
_WIN32_ALT_PRESSED = 0x0003

_URL_PATTERN = re.compile(r"https?://[^\s<>{}\[\]()]+", re.IGNORECASE)
_IMAGE_SUFFIXES = (".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp")
_THINKING_BEAM_SPEED = 1.6
_THINKING_BEAM_TRAIL = 2.4
_THINKING_BEAM_GAP = 1.2


def _image_urls(text: str) -> list[str]:
    """Return unique image URLs without changing the persisted answer."""

    urls: list[str] = []
    for match in _URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(".,;:!?，。；：！？")
        if url and _is_image_url(url) and url not in urls:
            urls.append(url)
    return urls


def _is_image_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    path = unquote(parsed.path).casefold()
    if path.endswith(_IMAGE_SUFFIXES):
        return True
    filenames = parse_qs(parsed.query).get("filename", [])
    return any(unquote(value).casefold().endswith(_IMAGE_SUFFIXES) for value in filenames)


def _image_link_suffix(index: int, total: int) -> str:
    """Keep multi-image links distinct and expose the keyboard fallback once."""

    position = f" {index}/{total}" if total > 1 else ""
    fallback = "  ·  /open" if index == total else ""
    return position + fallback


def _legacy_prompt_markup(console: Console) -> str:
    """Use the styled arrow in Unicode consoles and an ASCII prompt otherwise."""

    marker = "»"
    if not _console_supports(console, marker):
        marker = ">"
    return f"\n[bold yellow]{marker}[/bold yellow]"


def _console_supports(console: Console, text: str) -> bool:
    encoding = getattr(console, "encoding", None) or getattr(sys.stdout, "encoding", None)
    if not encoding:
        return True
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _configure_unicode_stdio() -> None:
    """Keep Windows terminal streams from falling back to the GBK locale."""

    if sys.platform != "win32":
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Embedded consoles can expose a read-only stream wrapper.
            continue


def _normalise_unicode_text(text: str) -> str:
    """Collapse Win32 UTF-16 surrogate pairs into real Unicode code points."""

    if not any(0xD800 <= ord(character) <= 0xDFFF for character in text):
        return text
    return text.encode("utf-16le", errors="surrogatepass").decode(
        "utf-16le",
        errors="replace",
    )


def _render_answer_lines(answer: str, width: int) -> tuple[list[str], list[str]]:
    """Render Markdown to stable plain lines for both TUI and legacy output."""

    visible, urls = _answer_without_image_urls(answer)
    buffer = io.StringIO()
    render_console = Console(
        file=buffer,
        width=max(1, width),
        color_system=None,
        force_terminal=False,
    )
    if visible:
        render_console.print(Markdown(visible))
    body = buffer.getvalue().rstrip()
    lines = [line.rstrip() for line in body.splitlines()] if body else []
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines, urls


def _normalise_answer_lines(lines: list[str]) -> list[str]:
    """Avoid a doubled outer bullet when Markdown starts with a list."""

    normalised = list(lines)
    if normalised and normalised[0].lstrip().startswith("• "):
        normalised[0] = normalised[0].lstrip()[2:]
    return normalised


def _answer_without_image_urls(answer: str) -> tuple[str, list[str]]:
    """Build compact display copy while leaving the stored answer untouched."""

    urls = _image_urls(answer)
    visible = answer
    for url in urls:
        visible = visible.replace(url, "")
    cleaned_lines = []
    for line in visible.splitlines():
        compact = line.strip()
        if compact in {"-", "*", "•", "[]", "![]", "()"}:
            continue
        # A Markdown link loses its target above. Remove leftover empty syntax;
        # the real target is rendered as a dedicated terminal link below.
        line = re.sub(r"!?\[([^\]]*)\]\(\s*\)", r"\1", line)
        cleaned_lines.append(line.rstrip())
    return "\n".join(cleaned_lines).strip(), urls


def _copy_to_clipboard(text: str) -> str | None:
    """Copy text through an installed native clipboard helper.

    Clipboard support is deliberately optional: the CLI remains usable over
    SSH, in containers, and in minimal CI images where no helper exists.
    Return the executable used so callers can give a useful confirmation.
    """

    text = _normalise_unicode_text(text)
    if not text:
        return None
    if sys.platform == "win32":
        commands = [("clip.exe",)]
    elif sys.platform == "darwin":
        commands = [("pbcopy",)]
    else:
        commands = [
            ("wl-copy",),
            ("xclip", "-selection", "clipboard"),
            ("xsel", "--clipboard", "--input"),
        ]
    for command in commands:
        executable = shutil.which(command[0])
        if executable is None:
            continue
        try:
            if sys.platform == "win32":
                # clip.exe consumes UTF-16LE from redirected stdin. Passing a
                # Python ``str`` would use the process GBK locale and fail on emoji.
                input_data: str | bytes = text.encode("utf-16le", errors="replace")
                text_mode = False
            else:
                input_data = text
                text_mode = True
            subprocess.run(
                [executable, *command[1:]],
                input=input_data,
                text=text_mode,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return executable
    return None


def _is_native_shift_enter(record) -> bool:  # noqa: ANN001
    if getattr(record, "VirtualKeyCode", None) != 13:
        return False
    state = getattr(record, "ControlKeyState", 0)
    return bool(state & _WIN32_SHIFT_PRESSED) and not bool(
        state & (_WIN32_CTRL_PRESSED | _WIN32_ALT_PRESSED)
    )


if ConsoleInputReader is not None:

    class _ShiftAwareConsoleInputReader(ConsoleInputReader):
        """Keep Win32's Shift+Enter modifier before prompt_toolkit drops it."""

        def _event_to_key_presses(self, event):  # noqa: ANN001
            if _is_native_shift_enter(event):
                return [KeyPress(Keys.ControlM, "\x1b[27;2;13~")]
            return super()._event_to_key_presses(event)

else:
    _ShiftAwareConsoleInputReader = None


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
    _IDLE_STATUS = "● 就绪"

    def __init__(self, owner: LeonConsole) -> None:
        if Application is None:
            raise RuntimeError("prompt_toolkit is not installed")
        self.owner = owner
        self.blocks: list[str] = []
        self.lock = threading.RLock()
        self.busy = False
        self.status_text = self._IDLE_STATUS
        self._status_animated = False
        self._status_animation_started_at: float | None = None
        self._latest_image_url: str | None = None
        self._model_picker: tuple[str, ...] | None = None
        self._follow_output = True
        self._generation = 0
        self._active_cancel_event: threading.Event | None = None
        self._active_thread: threading.Thread | None = None
        self._exit_requested = False
        self._started_at: float | None = None
        self._last_ctrl_c_at: float | None = None
        self._ctrl_c_count = 0
        self._ctrl_c_clear_in_progress = False
        self._history_entries: list[str] = []
        self._history_index = 0
        self._history_draft = ""
        self._history_setting = False

        self.output_control = FormattedTextControl(
            self._output_fragments,
            focusable=False,
            show_cursor=False,
            get_cursor_position=self._output_cursor_position,
        )
        self.output = Window(
            content=self.output_control,
            height=Dimension(weight=1),
            wrap_lines=True,
            always_hide_cursor=True,
            right_margins=[ScrollbarMargin(display_arrows=False)],
        )
        input_kwargs = {
            "height": Dimension(min=1, max=6),
            "dont_extend_height": True,
            "prompt": [("class:composer.prompt", "  » ")],
            "get_line_prefix": self._composer_line_prefix,
            "multiline": True,
            "accept_handler": self._accept,
            "style": "class:composer.input",
        }
        if InMemoryHistory is not None:
            input_kwargs["history"] = self._build_input_history()
        if WordCompleter is not None:
            input_kwargs["completer"] = WordCompleter(
                _CLI_COMMANDS,
                ignore_case=True,
                meta_dict=_CLI_COMMAND_META,
                sentence=True,
            )
        self.input = TextArea(**input_kwargs)
        self.input.buffer.on_text_changed += self._on_input_text_changed
        self.status = Window(
            content=FormattedTextControl(self._status_fragments),
            height=self._status_height,
            style="class:status",
        )
        self.composer_top = Window(
            height=1,
            char="─",
            style="class:composer.line",
        )
        self.composer_bottom = Window(
            height=1,
            char="─",
            style="class:composer.line",
        )
        self.bottom_bar = Window(
            content=FormattedTextControl(self._bottom_bar_fragments),
            height=1,
            style="class:bottom",
        )
        key_bindings = KeyBindings()
        input_focused = has_focus(self.input)
        turn_busy = Condition(lambda: self.busy)
        model_picker_active = Condition(
            lambda: self._model_picker is not None and not self.busy
        )

        @key_bindings.add("enter", filter=input_focused, eager=True)
        def _(event) -> None:  # noqa: ANN001
            self._handle_enter(event)

        @key_bindings.add("tab", filter=input_focused, eager=True)
        def _(event) -> None:  # noqa: ANN001
            buffer = event.current_buffer
            if buffer.complete_state is None:
                buffer.start_completion(select_first=False)
            else:
                buffer.complete_next()

        @key_bindings.add("up", filter=input_focused, eager=True)
        def _(event) -> None:  # noqa: ANN001
            self._history_or_cursor(event.current_buffer, direction=-1)

        @key_bindings.add("down", filter=input_focused, eager=True)
        def _(event) -> None:  # noqa: ANN001
            self._history_or_cursor(event.current_buffer, direction=1)

        @key_bindings.add("c-p", filter=input_focused, eager=True)
        def _(event) -> None:  # noqa: ANN001
            self._history_or_cursor(event.current_buffer, direction=-1)

        @key_bindings.add("c-n", filter=input_focused, eager=True)
        def _(event) -> None:  # noqa: ANN001
            self._history_or_cursor(event.current_buffer, direction=1)

        # prompt_toolkit 3.x does not yet decode Kitty's CSI-u Shift+Enter.
        # Register its raw key sequence so supported terminals still get the
        # expected composer behavior without changing global input parsing.
        @key_bindings.add(
            "escape",
            "[",
            "1",
            "3",
            ";",
            "2",
            "u",
            filter=input_focused,
            eager=True,
        )
        @key_bindings.add(
            "escape",
            "[",
            "1",
            "3",
            ";",
            "5",
            "u",
            filter=input_focused,
            eager=True,
        )
        @key_bindings.add("escape", "c-j", filter=input_focused, eager=True)
        @key_bindings.add("c-j", filter=input_focused, eager=True)
        @key_bindings.add("escape", "enter", filter=input_focused, eager=True)
        def _(event) -> None:  # noqa: ANN001
            self._insert_newline(event.current_buffer)

        @key_bindings.add("c-c")
        def _(event) -> None:  # noqa: ANN001
            self._handle_ctrl_c(event)

        @key_bindings.add("c-insert")
        def _(event) -> None:  # noqa: ANN001, ARG001
            if self.owner.copy_last_answer(quiet=True):
                self._set_status("● 已复制上一条回答")
            else:
                self._set_status("● 当前没有可复制的回答")

        @key_bindings.add("escape", filter=turn_busy)
        def _(event) -> None:  # noqa: ANN001
            self._handle_interrupt(event, exit_after=False)

        @key_bindings.add("escape", filter=model_picker_active)
        def _(event) -> None:  # noqa: ANN001, ARG001
            self.cancel_model_picker()

        @key_bindings.add("c-d", filter=input_focused, eager=True)
        def _(event) -> None:  # noqa: ANN001
            self._handle_eof(event)

        @key_bindings.add("c-u", filter=input_focused, eager=True)
        def _(event) -> None:  # noqa: ANN001
            self._clear_input(event.current_buffer)

        @key_bindings.add("c-k", filter=input_focused, eager=True)
        def _(event) -> None:  # noqa: ANN001
            self._delete_to_end(event.current_buffer)

        @key_bindings.add("c-w", filter=input_focused, eager=True)
        def _(event) -> None:  # noqa: ANN001
            self._delete_previous_word(event.current_buffer)

        @key_bindings.add("c-l")
        def _(event) -> None:  # noqa: ANN001
            self.clear_output()
            self._set_status(self.status_text, animate=self._status_animated)

        root = HSplit(
            [
                self.output,
                self.status,
                self.composer_top,
                self.input,
                self.composer_bottom,
                self.bottom_bar,
            ]
        )
        tui_style = Style.from_dict(
            {
                "message.user": "#DCEEFF",
                "message.assistant": "#DCEEFF",
                "message.marker": "#DCEEFF",
                "message.tool": "#71869A",
                "message.link": "underline #73B8FF",
                "status": "#71869A",
                "status.pulse.hot": "bold #FFFFFF",
                "status.pulse.bright": "#DCEEFF",
                "status.pulse.mid": "#AFC4D6",
                "status.pulse.soft": "#899EAF",
                "status.pulse.dim": "#516579",
                "status.cancel": "#FF8FB1",
                "composer.line": "#15304A",
                "composer.prompt": "bold #F5C26B",
                "composer.input": "#DCEEFF",
                "composer.hint": "#71869A",
                "bottom": "#5F7488",
                "bottom.model": "#AFC4D6",
                "bottom.provider": "#71869A",
                "bottom.path": "#71869A",
                "bottom.meta": "#5F7488",
            }
        )
        application_kwargs = {
            "layout": Layout(root, focused_element=self.input),
            "key_bindings": key_bindings,
            "full_screen": True,
            "mouse_support": True,
            "style": tui_style,
            "refresh_interval": 1 / 12,
        }
        native_input = self._create_native_input()
        if native_input is not None:
            application_kwargs["input"] = native_input
        self.app = Application(**application_kwargs)

    @staticmethod
    def available() -> bool:
        return Application is not None

    @staticmethod
    def _create_native_input():
        if (
            sys.platform != "win32"
            or not sys.stdin.isatty()
            or create_input is None
            or Win32Input is None
            or ConsoleInputReader is None
            or _ShiftAwareConsoleInputReader is None
        ):
            return None
        try:
            input_adapter = create_input()
        except OSError:
            return None
        reader = getattr(input_adapter, "console_input_reader", None)
        if not isinstance(input_adapter, Win32Input) or not isinstance(
            reader, ConsoleInputReader
        ):
            input_adapter.close()
            return None
        try:
            replacement = _ShiftAwareConsoleInputReader()
        except OSError:
            input_adapter.close()
            return None
        reader.close()
        input_adapter.console_input_reader = replacement
        return input_adapter

    @staticmethod
    def _composer_line_prefix(line_number: int, wrap_count: int):  # noqa: ARG004
        if line_number == 0 and wrap_count == 0:
            return ""
        return [("class:composer.prompt", "    ")]

    def _build_input_history(self):
        history = InMemoryHistory()
        store = getattr(self.owner, "store", None)
        session_id = getattr(self.owner, "session_id", "")
        if store is None or not session_id:
            return history
        try:
            messages = store.load_messages(session_id)
        except (OSError, ValueError):
            return history
        for item in messages:
            if item["role"] == "user" and item["content"].strip():
                history.append_string(item["content"])
        self._history_entries = history.get_strings()
        self._history_index = len(self._history_entries)
        self._history_draft = ""
        return history

    def _on_input_text_changed(self, buffer) -> None:  # noqa: ANN001
        if self._history_setting:
            return
        with self.lock:
            self._history_index = len(self._history_entries)
            self._history_draft = buffer.text
            # A normal edit breaks a consecutive Ctrl+C sequence. The first
            # Ctrl+C clears the draft through the guarded path below, so that
            # intentional clear does not turn the next press into a reset.
            if not self._ctrl_c_clear_in_progress:
                self._last_ctrl_c_at = None
                self._ctrl_c_count = 0
                if self.status_text.startswith("● "):
                    self.status_text = self._IDLE_STATUS
                    self._status_animated = False

    def _set_history_text(self, buffer, text: str) -> None:  # noqa: ANN001
        self._history_setting = True
        try:
            buffer.text = text
            buffer.cursor_position = len(buffer.text)
        finally:
            self._history_setting = False

    def _history_or_cursor(self, buffer, *, direction: int) -> None:  # noqa: ANN001
        with self.lock:
            self._last_ctrl_c_at = None
            self._ctrl_c_count = 0
            if self.status_text.startswith("● "):
                self.status_text = self._IDLE_STATUS
                self._status_animated = False
        if buffer.complete_state is not None:
            if direction < 0:
                buffer.complete_previous()
            else:
                buffer.complete_next()
            return
        if buffer.document.line_count > 1:
            if direction < 0:
                buffer.cursor_up()
            else:
                buffer.cursor_down()
            return
        with self.lock:
            entries = self._history_entries
            index = self._history_index
            draft = self._history_draft
        if not entries:
            return
        if direction < 0:
            if index == len(entries):
                draft = buffer.text
            if index > 0:
                index -= 1
                self._set_history_text(buffer, entries[index])
        else:
            if index < len(entries) - 1:
                index += 1
                self._set_history_text(buffer, entries[index])
            elif index == len(entries) - 1:
                index = len(entries)
                self._set_history_text(buffer, draft)
            else:
                self._set_history_text(buffer, "")
        with self.lock:
            self._history_index = index
            self._history_draft = draft

    def refresh_input_history(self) -> None:
        if InMemoryHistory is None:
            return
        history = self._build_input_history()

        def replace_history() -> None:
            document = self.input.buffer.document
            self.input.buffer.history = history
            self._history_entries = history.get_strings()
            self._history_index = len(self._history_entries)
            self._history_draft = ""
            self.input.buffer.reset(document=document)
            self.app.invalidate()

        loop = getattr(self.app, "loop", None)
        if getattr(self.app, "_is_running", False) and loop is not None:
            loop.call_soon_threadsafe(replace_history)
            return
        replace_history()

    def _bottom_bar_fragments(self):
        hint = self._composer_hint_fragments()
        if hint:
            return hint
        model = getattr(self.owner, "llm_model", "-") or "-"
        provider = getattr(self.owner, "llm_provider_name", "-") or "-"
        session = getattr(self.owner, "session_id", "-") or "-"
        fragments = [
            ("class:bottom.model", f"  {model}"),
            ("class:bottom.meta", "  "),
            ("class:bottom.provider", provider),
            ("class:bottom.meta", "  "),
            ("class:bottom.path", str(Path.cwd())),
            ("class:bottom.meta", f"  ·  session {session[:8]}"),
        ]
        if getattr(self.owner, "_last_answer", ""):
            fragments.extend(
                [
                    ("class:bottom.meta", "  ·  "),
                    ("class:bottom.model", "Ctrl+Ins 复制"),
                ]
            )
        return fragments

    def _composer_hint_fragments(self):
        with self.lock:
            busy = self.busy
            model_picker = self._model_picker
        text = getattr(getattr(self, "input", None), "text", "")
        if model_picker is not None:
            hint = "输入模型序号或完整 ID · Enter 确认 · esc 取消"
        elif not text:
            return []
        elif busy:
            hint = "当前轮处理中 · 草稿会保留 · esc 取消"
        elif text.lstrip().startswith("/"):
            hint = "Tab 补全命令 · Enter 执行"
        else:
            hint = "Enter 发送 · Shift+Enter/Ctrl+J 换行 · Tab 补全"
        return [("class:composer.hint", f"  {hint}")]

    def _status_height(self) -> Dimension:
        with self.lock:
            visible = (
                self.busy
                or self._model_picker is not None
                or self.status_text != self._IDLE_STATUS
            )
        return Dimension.exact(1 if visible else 0)

    def _status_fragments(self):
        with self.lock:
            status = self.status_text
            animated = self._status_animated
            started_at = self._started_at
            animation_started_at = self._status_animation_started_at
            busy = self.busy
            model_picker = self._model_picker
        if busy and started_at is not None:
            now = monotonic()
            elapsed = self._format_elapsed(now - started_at)
            if status.startswith("⏹"):
                return [
                    ("class:status.cancel", f"  ◦ {status}"),
                    ("class:status", f" ({elapsed})"),
                ]
            fragments = [("class:status", "  ◦ ")]
            animation_origin = (
                animation_started_at
                if animation_started_at is not None
                else now
            )
            animation_elapsed = max(0.0, now - animation_origin)
            beam_span = max(1.0, len(status) + _THINKING_BEAM_GAP)
            beam_position = (animation_elapsed * _THINKING_BEAM_SPEED) % beam_span
            for index, character in enumerate(status):
                if animated and beam_position <= len(status) - 1:
                    intensity = max(
                        0.0,
                        1.0 - abs(index - beam_position) / _THINKING_BEAM_TRAIL,
                    )
                    if intensity >= 0.86:
                        style = "class:status.pulse.hot"
                    elif intensity >= 0.62:
                        style = "class:status.pulse.bright"
                    elif intensity >= 0.38:
                        style = "class:status.pulse.mid"
                    elif intensity >= 0.15:
                        style = "class:status.pulse.soft"
                    else:
                        style = "class:status.pulse.dim"
                else:
                    style = "class:status.pulse.dim"
                fragments.append((style, character))
            fragments.append(("class:status", f" ({elapsed} • esc 取消)"))
            return fragments
        if model_picker is not None:
            return [("class:status", "  ◦ 选择模型：输入序号或完整 ID · esc 取消")]
        if status == self._IDLE_STATUS:
            return []
        style = "class:status.cancel" if status.startswith("⏹") else "class:status"
        return [(style, f"  {status}")]

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, remainder = divmod(total, 60)
        if minutes:
            return f"{minutes}m {remainder:02d}s"
        return f"{remainder}s"

    def _status_line(self) -> str:
        with self.lock:
            status = self.status_text
            started_at = self._started_at
            busy = self.busy
            model_picker = self._model_picker
        if busy and started_at is not None:
            elapsed = self._format_elapsed(monotonic() - started_at)
            suffix = "" if status.startswith("⏹") else " • esc 取消"
            return f"◦ {status} ({elapsed}{suffix})"
        if model_picker is not None:
            return "◦ 选择模型：输入序号或完整 ID · esc 取消"
        if status == self._IDLE_STATUS:
            return ""
        return status

    def _rendered_blocks(self) -> str:
        with self.lock:
            return "\n\n".join(self.blocks).rstrip() + ("\n" if self.blocks else "")

    def _output_cursor_position(self):
        rendered = self._rendered_blocks()
        line_count = max(1, rendered.count("\n"))
        if self._follow_output:
            return Point(x=0, y=line_count - 1)
        current_scroll = max(0, int(getattr(self.output, "vertical_scroll", 0)))
        return Point(x=0, y=min(line_count - 1, current_scroll))

    def _observe_output_mouse(self, event):  # noqa: ANN001
        if event.event_type == MouseEventType.SCROLL_UP:
            self._follow_output = False
        elif event.event_type == MouseEventType.SCROLL_DOWN:
            info = getattr(self.output, "render_info", None)
            if (
                info is not None
                and info.vertical_scroll + info.window_height >= info.content_height
            ):
                self._follow_output = True
        return NotImplemented

    def _open_url(self, url: str) -> None:
        try:
            opened = webbrowser.open(url, new=2)
        except (OSError, webbrowser.Error) as exc:
            self.write_plain(f"⚠ 无法打开图片：{type(exc).__name__}: {exc}")
            return
        if not opened:
            self.write_plain("⚠ 系统没有可用的浏览器打开图片链接。")

    def _link_handler(self, url: str):
        def handle(event) -> None:  # noqa: ANN001
            if (
                event.event_type == MouseEventType.MOUSE_UP
                and event.button == MouseButton.LEFT
            ):
                self._open_url(url)
                return None
            return self._observe_output_mouse(event)

        return handle

    def _output_fragments(self):
        rendered = self._rendered_blocks()
        if not rendered:
            return [("class:status", "\n", self._observe_output_mouse)]
        fragments = []
        continuation_style = ""
        for line in rendered.splitlines(keepends=True):
            line_without_newline = line.rstrip("\n")
            cursor = 0
            if not line_without_newline:
                base_style = ""
                continuation_style = ""
            elif line_without_newline.startswith("» "):
                base_style = "class:message.user"
                continuation_style = base_style
                fragments.append(
                    ("class:composer.prompt", "» ", self._observe_output_mouse)
                )
                cursor = 2
            elif line_without_newline.startswith("• "):
                base_style = "class:message.assistant"
                continuation_style = base_style
                fragments.append(
                    ("class:message.marker", "• ", self._observe_output_mouse)
                )
                cursor = 2
            elif line_without_newline.startswith(("● ", "✓ ", "✗ ", "◦ ", "⏹ ", "⚠ ")):
                base_style = "class:message.tool"
                continuation_style = base_style
            elif line_without_newline.startswith("  ") and continuation_style:
                base_style = continuation_style
            else:
                base_style = ""
                continuation_style = ""
            for match in _URL_PATTERN.finditer(line_without_newline):
                if match.end() <= cursor:
                    continue
                url = match.group(0).rstrip(".,;:!?，。；：！？")
                if not _is_image_url(url):
                    continue
                if match.start() > cursor:
                    fragments.append(
                        (
                            base_style,
                            line_without_newline[cursor : match.start()],
                            self._observe_output_mouse,
                        )
                    )
                fragments.append(("class:message.link", "↗ 打开图片", self._link_handler(url)))
                cursor = match.end()
            if cursor < len(line_without_newline):
                fragments.append(
                    (base_style, line_without_newline[cursor:], self._observe_output_mouse)
                )
            if line.endswith("\n"):
                fragments.append((base_style, "\n", self._observe_output_mouse))
        return fragments

    def run(self) -> None:
        self.owner.ui = self
        try:
            self.owner._print_startup()
            if getattr(self.owner, "_resumed_session", False):
                self.owner._print_resume_context()
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
            thread.join(timeout=1.0)

    def write_rich(self, *objects: object, **kwargs: object) -> None:
        buffer = io.StringIO()
        render_console = Console(
            file=buffer,
            width=self._render_width(),
            color_system=None,
            force_terminal=False,
        )
        render_console.print(*objects, **kwargs)
        text = buffer.getvalue().rstrip()
        if text:
            self.write_plain(text)

    def _render_width(self) -> int:
        render_info = getattr(self.output, "render_info", None)
        width = getattr(render_info, "window_width", 0)
        if not width:
            width = shutil.get_terminal_size(fallback=(100, 24)).columns
        return min(100, max(1, width - 2))

    def write_answer(self, answer: str) -> None:
        lines, urls = _render_answer_lines(answer, self._render_width())
        lines = _normalise_answer_lines(lines)
        lines.extend(
            url + _image_link_suffix(index, len(urls))
            for index, url in enumerate(urls, start=1)
        )
        if not lines:
            lines = ["（空回答）"]
        parts = [
            ("• " if index == 0 else "  ") + line
            for index, line in enumerate(lines)
        ]
        if urls:
            self._latest_image_url = urls[-1]
        self.write_plain("\n".join(parts))

    def write_plain(self, text: str) -> None:
        cleaned = _normalise_unicode_text(text).rstrip()
        if not cleaned:
            return
        with self.lock:
            self.blocks.append(cleaned)
            if len(self.blocks) > self._MAX_BLOCKS:
                self.blocks = self.blocks[-self._MAX_BLOCKS :]
            urls = _image_urls(cleaned)
            if urls:
                self._latest_image_url = urls[-1]
            self._follow_output = True
        self.app.invalidate()

    def clear_output(self) -> None:
        with self.lock:
            self.blocks.clear()
            self._latest_image_url = None
            self._follow_output = True
        self.app.invalidate()

    def write_user_message(self, message: str) -> None:
        lines = message.splitlines() or [""]
        body = "\n".join(("» " if index == 0 else "  ") + line for index, line in enumerate(lines))
        self.write_plain(body)

    def open_latest_image(self) -> None:
        url = self._latest_image_url
        if not url:
            self.write_plain("[没有可打开的图片]")
            return
        self._open_url(url)

    def begin_model_picker(self, models: Sequence[str], *, current: str) -> None:
        choices = tuple(dict.fromkeys(model for model in models if model))
        if not choices:
            return
        with self.lock:
            self._model_picker = choices
        lines = ["• 选择模型"]
        for index, model in enumerate(choices, start=1):
            marker = "  current" if model == current else ""
            lines.append(f"  {index:>2}. {model}{marker}")
        lines.append("  default. 跟随当前 provider 默认模型")
        self.write_plain("\n".join(lines))
        self.app.invalidate()

    def cancel_model_picker(self, *, silent: bool = False) -> None:
        with self.lock:
            was_active = self._model_picker is not None
            self._model_picker = None
        if was_active and not silent:
            self.write_plain("◦ 已取消模型选择")
        self._set_status(self._IDLE_STATUS)

    def _accept_model_choice(self, buffer, candidate: str) -> bool:  # noqa: ANN001
        buffer.text = ""
        with self.lock:
            self._model_picker = None
        self.owner.switch_model(candidate)
        self._set_status(self._IDLE_STATUS)
        return False

    def is_current_turn(self, generation: int, cancel_event: threading.Event) -> bool:
        with self.lock:
            return (
                self.busy
                and self._generation == generation
                and self._active_cancel_event is cancel_event
            )

    def _handle_ctrl_c(self, event) -> None:  # noqa: ANN001
        """Clear input first; an idle empty session exits with one Ctrl+C."""

        buffer = getattr(event, "current_buffer", None)
        if buffer is None:
            input_widget = getattr(self, "input", None)
            buffer = getattr(input_widget, "buffer", None)
        draft = getattr(buffer, "text", "") if buffer is not None else ""
        with self.lock:
            busy = self.busy
            model_picker = self._model_picker
            idle_empty = not busy and not draft and model_picker is None
            now = monotonic()
            if self._last_ctrl_c_at is None or now - self._last_ctrl_c_at > 1.5:
                self._ctrl_c_count = 0
            self._ctrl_c_count += 1
            self._last_ctrl_c_at = now
            count = self._ctrl_c_count

        if idle_empty:
            event.app.exit()
            return

        if count == 1:
            if draft:
                with self.lock:
                    self._ctrl_c_clear_in_progress = True
                try:
                    buffer.text = ""
                finally:
                    with self.lock:
                        self._ctrl_c_clear_in_progress = False
                self._set_status("● 输入已清空 · ↑/↓ 历史")
            elif model_picker:
                self.cancel_model_picker()
            else:
                self._set_status("● 再按一次打断 · 第三次退出")
            return

        if count == 2:
            if busy:
                self._handle_interrupt(
                    event,
                    exit_after=False,
                    message_override="⏹ 本轮已取消；再次按 Ctrl+C 退出。",
                    status_override="⏹ 已取消 · 再按 Ctrl+C 退出",
                )
            else:
                self._set_status("● 再按一次退出")
            return

        if busy:
            self._handle_interrupt(event, exit_after=True)
        else:
            event.app.exit()

    def _handle_interrupt(
        self,
        event,
        *,
        exit_after: bool,
        message_override: str | None = None,
        status_override: str | None = None,
    ) -> None:  # noqa: ANN001
        with self.lock:
            cancel_event = self._active_cancel_event
            active = self.busy and cancel_event is not None
        if not active:
            if exit_after:
                event.app.exit()
            else:
                self._set_status(self._IDLE_STATUS)
            return

        already_requested = cancel_event.is_set()
        self._set_cancel_event(cancel_event)
        message = message_override or "⏹ 已请求取消当前轮；界面保持可用。"
        status = status_override or "⏹ 取消中 · 迟到结果会被丢弃"
        with self.lock:
            still_current = self._active_cancel_event is cancel_event and self.busy
            if still_current and exit_after:
                self._exit_requested = True
            should_exit = exit_after and not still_current
            exit_requested = still_current and self._exit_requested
            if exit_requested:
                message = "⏹ 已请求取消；当前请求收敛后退出。"
                status = "⏹ 取消中 · 等待当前同步边界收敛后退出"
            elif already_requested:
                message = "⏹ 本轮已在取消中，等待结果收敛…"
                status = "⏹ 取消中 · 迟到结果会被丢弃"
        if should_exit:
            event.app.exit()
            return
        if not still_current:
            self._set_status(self._IDLE_STATUS)
            return
        self.write_plain(message)
        self._set_status(status)

    def _handle_eof(self, event) -> None:  # noqa: ANN001
        buffer = event.current_buffer
        if buffer.text:
            buffer.delete()
            return
        self._handle_interrupt(event, exit_after=True)

    @staticmethod
    def _clear_input(buffer) -> None:  # noqa: ANN001
        before_cursor = buffer.document.text_before_cursor
        line_prefix = before_cursor.rsplit("\n", 1)[-1]
        line_suffix = buffer.document.text_after_cursor.split("\n", 1)[0]
        buffer.delete_before_cursor(count=len(line_prefix))
        buffer.delete(count=len(line_suffix))

    @staticmethod
    def _delete_to_end(buffer) -> None:  # noqa: ANN001
        line_suffix = buffer.document.text_after_cursor.split("\n", 1)[0]
        buffer.delete(count=len(line_suffix))

    @staticmethod
    def _delete_previous_word(buffer) -> None:  # noqa: ANN001
        line_before_cursor = buffer.document.text_before_cursor.rsplit("\n", 1)[-1]
        trimmed = line_before_cursor.rstrip()
        if not trimmed:
            buffer.delete_before_cursor(count=len(line_before_cursor))
            return
        word_start = len(trimmed)
        while word_start and not trimmed[word_start - 1].isspace():
            word_start -= 1
        buffer.delete_before_cursor(count=len(line_before_cursor) - word_start)

    def _set_cancel_event(self, cancel_event: threading.Event) -> None:
        commit_lock = getattr(self.owner, "_commit_lock", None)
        if commit_lock is None:
            cancel_event.set()
            return
        with commit_lock:
            cancel_event.set()

    @staticmethod
    def _is_newline_shortcut(event) -> bool:  # noqa: ANN001
        key_sequence = getattr(event, "key_sequence", ())
        if not key_sequence:
            return False
        return getattr(key_sequence[-1], "data", "") in _NEWLINE_ENTER_DATA

    @staticmethod
    def _insert_newline(buffer) -> None:  # noqa: ANN001
        buffer.newline(copy_margin=False)

    def _handle_enter(self, event) -> None:  # noqa: ANN001
        if self._is_newline_shortcut(event):
            self._insert_newline(event.current_buffer)
            return
        with self.lock:
            busy = self.busy
            model_picker = self._model_picker
        if model_picker is not None:
            self._accept(event.current_buffer)
            return
        if busy:
            # Avoid adding an unsent draft to prompt_toolkit history while the
            # current turn is still running.
            self._accept(event.current_buffer)
            return
        event.current_buffer.validate_and_handle()

    def _accept(self, buffer) -> bool:  # noqa: ANN001
        message = _normalise_unicode_text(buffer.text.strip("\r\n"))
        if not message.strip():
            buffer.text = ""
            return False
        with self.lock:
            self._last_ctrl_c_at = None
            model_picker = self._model_picker
            if self.busy:
                busy = True
            else:
                busy = False
        if model_picker is not None:
            if message.lstrip().startswith("/"):
                self.cancel_model_picker(silent=True)
            else:
                return self._accept_model_choice(buffer, message.strip())
        if busy:
            self.write_plain(
                "⏳ 上一轮仍在处理；草稿已保留。按 Esc 取消，"
                "或按 Ctrl+C 清输入 → 打断 → 退出。"
            )
            return True
        if buffer.text != message:
            # Keep the history entry identical to the text sent to the agent;
            # a final Shift+Enter should not leave a phantom blank line.
            buffer.text = message
        with self.lock:
            self._history_entries.append(message)
            self._history_index = len(self._history_entries)
            self._history_draft = ""
        self.write_user_message(message)
        if message.casefold() in {"/exit", "/quit"}:
            self.app.exit()
            return False

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
                daemon=True,
            )
            self._active_thread = thread
        self._set_status("正在思考中", animate=True)
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.busy = False
                self._active_cancel_event = None
                self._active_thread = None
                self._started_at = None
            raise
        return False

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
                    self._set_status("👋 正在退出")
                    self.app.exit()
                else:
                    self._set_status(self._IDLE_STATUS)

    def _set_status(self, text: str, *, animate: bool = False) -> None:
        with self.lock:
            if animate and (not self._status_animated or self.status_text != text):
                self._status_animation_started_at = monotonic()
            elif not animate:
                self._status_animation_started_at = None
            self.status_text = text
            self._status_animated = animate
        self.app.invalidate()


class LeonConsole:
    def __init__(self, args: argparse.Namespace) -> None:
        self.console = Console()
        self.ui: TerminalChatUI | None = None
        self.leon_config_file = apply_config_file()
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
        self.memory_store = MemoryStore(self.config.session_db)
        self.session_id = self._resolve_session(args)
        self._resumed_session = bool(getattr(args, "session", None) and not args.new)
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
        self._last_user_message = ""
        self._last_answer = ""
        self._last_image_url: str | None = None
        self._restore_last_exchange()
        self._progress: Progress | None = None
        self._progress_task_id: int | None = None
        self._image_progress_active = False
        self._commit_lock = threading.Lock()
        self.agent = self._create_agent()

    def _resolve_session(self, args: argparse.Namespace) -> str:
        if args.session and not args.new:
            if not self.store.has_session(args.session):
                raise ValueError(f"Session not found: {args.session}")
            return args.session
        return self.store.create_session()

    def _restore_last_exchange(self) -> None:
        messages = self.store.load_messages(self.session_id)
        self._last_user_message = next(
            (
                item["content"]
                for item in reversed(messages)
                if item["role"] == "user"
            ),
            "",
        )
        self._last_answer = next(
            (
                item["content"]
                for item in reversed(messages)
                if item["role"] == "assistant"
            ),
            "",
        )
        urls = _image_urls(self._last_answer)
        self._last_image_url = urls[-1] if urls else None

    def _print_resume_context(self) -> None:
        ui = getattr(self, "ui", None)
        if self._last_user_message:
            if ui is not None:
                ui.write_user_message(self._last_user_message)
            else:
                self.print(Text(f"» {self._last_user_message}", style="#DCEEFF"))
        if self._last_answer:
            self._print_answer(self._last_answer)

    def print(self, *objects: object, **kwargs: object) -> None:
        ui = getattr(self, "ui", None)
        if ui is not None:
            ui.write_rich(*objects, **kwargs)
            return
        self.console.print(*objects, **kwargs)

    def print_resume_hint(self) -> None:
        self.console.print("\n[dim]Leon Agent 已退出。[/dim]")
        self.console.print(
            f"[cyan]继续当前会话：[/cyan] leon resume {self.session_id}"
        )

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

    def _record_cancelled_tool_audit(
        self,
        exc: AgentCancelled,
        fallback_result: AgentResult | None = None,
    ) -> None:
        """Persist only completed, already-projected tool steps from a cancelled turn."""
        partial_result = getattr(exc, "partial_result", None)
        if not isinstance(partial_result, AgentResult) or not partial_result.steps:
            partial_result = fallback_result
        if not isinstance(partial_result, AgentResult) or not partial_result.steps:
            return
        audit_result = AgentResult(answer="", steps=list(partial_result.steps), messages=[])
        with self._commit_context():
            self.store.record_result(self.session_id, audit_result)

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
        self.search_service = create_search_service(
            api_key=(
                self.config.tavily_api_key.get_secret_value()
                if self.config.tavily_api_key
                else None
            ),
            base_url=self.config.tavily_base_url,
            timeout_seconds=self.config.tavily_timeout_seconds,
            max_results=self.config.tavily_max_results,
        )
        self.file_service = create_file_search_service(self.config.file_roots)
        self.file_write_service = create_file_write_service(self.config.file_roots)
        self.memory_service = MemoryService(
            self.memory_store,
            session_id=self.session_id,
        )
        self.direct_tools = create_leon_tools(
            self.image_client,
            session_id=self.session_id,
            default_mode_ids=self.config.default_mode_ids,
            search_service=self.search_service,
            file_service=self.file_service,
            file_write_service=self.file_write_service,
        )
        return LeonAgent(
            llm_client=llm_client,
            image_client=self.image_client,
            session_id=self.session_id,
            default_mode_ids=self.config.default_mode_ids,
            on_event=self._on_event,
            search_service=self.search_service,
            file_service=self.file_service,
            file_write_service=self.file_write_service,
            memory_service=self.memory_service,
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

    @staticmethod
    def _clip_startup(value: str, width: int) -> str:
        value = str(value or "-")
        if width <= 0:
            return ""
        if len(value) <= width:
            return value
        if width == 1:
            return value[:1]
        return value[: width - 1] + "~"

    def _startup_width(self) -> int:
        ui = getattr(self, "ui", None)
        if ui is not None:
            try:
                return max(1, int(ui._render_width()))
            except (AttributeError, TypeError, ValueError):
                pass
        width = getattr(getattr(self, "console", None), "width", 0)
        if not width:
            width = shutil.get_terminal_size(fallback=(100, 24)).columns
        return max(1, int(width) - 2)

    def _print_startup(self) -> None:
        width = self._startup_width()
        model = self.llm_model or "-"
        provider = self.llm_provider_name or self.llm_profile or "-"
        if width < 40:
            value_width = max(1, width - 2)
            compact = Text()
            compact.append(">_ LEON\n", style="bold #DCEEFF")
            compact.append(f"m {self._clip_startup(model, value_width)}\n", style="#DCEEFF")
            compact.append(
                f"p {self._clip_startup(provider, value_width)}\n",
                style="#DCEEFF",
            )
            compact.append(
                f"s {self._clip_startup(self.session_id[:8], value_width)}\n",
                style="#AFC4D6",
            )
            self.print(compact)
            return

        title = Text(">_ LEON AGENT", style="bold #DCEEFF")
        title.append("  v0.1.0", style="#71869A")
        body = Text()
        inner_width = max(1, width - 4)
        model = self._clip_startup(model, max(4, min(32, inner_width - 21)))
        provider = self._clip_startup(provider, max(4, inner_width - 10))
        body.append("model:     ", style="#71869A")
        body.append(model, style="#DCEEFF")
        body.append("\n", style="#DCEEFF")
        body.append("provider:  ", style="#71869A")
        body.append(f"{provider}\n", style="#DCEEFF")
        body.append("session:   ", style="#71869A")
        body.append(self.session_id[:16], style="#AFC4D6")

        self.print(
            Group(
                Panel(
                    body,
                    title=title,
                    border_style="#15304A",
                    padding=(0, 1),
                    expand=False,
                ),
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
        self._image_progress_active = True
        ui = getattr(self, "ui", None)
        if ui is not None:
            ui._set_status("正在生成图片", animate=True)
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
        active = bool(getattr(self, "_image_progress_active", False))
        ui = getattr(self, "ui", None)
        if not active and getattr(self, "_progress", None) is None:
            return
        self._image_progress_active = False
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
                ui._set_status("正在思考中", animate=True)
            return
        if event.kind == "assistant_delta":
            if ui is not None:
                ui._set_status("正在回答", animate=True)
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
        answer = _normalise_unicode_text(answer)
        self._last_answer = answer
        _, urls = _answer_without_image_urls(answer)
        if urls:
            self._last_image_url = urls[-1]
        ui = getattr(self, "ui", None)
        if ui is not None:
            ui.write_answer(answer)
            return
        width = max(1, min(100, int(getattr(self.console, "width", 100)) - 2))
        lines, urls = _render_answer_lines(answer, width)
        lines = _normalise_answer_lines(lines)
        marker = "•" if _console_supports(self.console, "•") else "*"
        link_label = "↗ 打开图片" if _console_supports(self.console, "↗") else "打开图片"
        output = Text()
        for index, line in enumerate(lines):
            if index:
                output.append("\n  ")
            else:
                output.append(f"{marker} ")
            output.append(line)
        for index, url in enumerate(urls, start=1):
            output.append("\n  ")
            output.append(link_label, style=f"underline #73B8FF link {url}")
            output.append(_image_link_suffix(index, len(urls)), style="dim")
        if not lines and not urls:
            output.append(f"{marker} （空回答）")
        # Let the terminal perform any visual wrapping. Rich must not inject a
        # newline into the hidden OSC 8 target carried by the short link.
        self.print(output, soft_wrap=True)

    def open_last_image(self) -> bool:
        url = getattr(self, "_last_image_url", None)
        if not url:
            self.print("[yellow]当前会话还没有可打开的图片。[/yellow]")
            return False
        try:
            opened = webbrowser.open(url, new=2)
        except (OSError, webbrowser.Error) as exc:
            self.print(f"[red]打开图片失败：{type(exc).__name__}: {exc}[/red]")
            return False
        if not opened:
            self.print("[yellow]系统没有可用的浏览器打开图片链接。[/yellow]")
            return False
        self.print("[green]↗ 已打开最近图片[/green]")
        return True

    def _start_llm_request(self) -> None:
        """Show feedback before the provider call, including in the legacy REPL."""
        self._check_active_turn()
        ui = getattr(self, "ui", None)
        if ui is not None:
            ui._set_status("正在思考中", animate=True)
            return
        marker = "✦" if _console_supports(self.console, "✦") else ">"
        self.print(f"[cyan]{marker}[/cyan] 正在思考中")

    def _format_request_error(self, exc: Exception) -> str:
        error_type = type(exc).__name__
        detail = str(exc).strip()
        if isinstance(exc, UnicodeError):
            return "终端文本编码失败（已切换 UTF-8），请重试；回答内容不会丢失。"
        if error_type in {"APITimeoutError", "TimeoutError", "ReadTimeout"}:
            timeout = float(getattr(self, "llm_timeout_seconds", 0.0) or 0.0)
            retries = getattr(self, "llm_max_retries", 0)
            if timeout <= 0:
                return (
                    f"模型连接阶段超时（响应等待不限时，自动重试 {retries} 次），"
                    "请检查 provider、模型 ID 或网络后重试。"
                )
            return (
                f"模型请求超时（{timeout:g}s，自动重试 {retries} 次），"
                "请检查 provider、模型 ID 或网络后重试。"
            )
        if error_type in {"APIConnectionError", "ConnectError", "ReadError"}:
            return "模型 provider 连接失败，请检查 base URL/网络后重试。"
        suffix = f": {detail}" if detail else ""
        return f"请求失败：{error_type}{suffix}"

    def process(self, message: str) -> bool:
        message = _normalise_unicode_text(message)
        stripped = message.strip()
        if not stripped:
            return True
        self._last_user_message = message
        result: AgentResult | None = None
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
            result.answer = _normalise_unicode_text(result.answer)
            with self._commit_context():
                self._check_active_turn()
                self.store.add_message(self.session_id, "user", message)
                self.store.record_result(self.session_id, result)
                self.store.add_message(self.session_id, "assistant", result.answer)
        except KeyboardInterrupt:
            self._stop_image_progress(ok=False)
            self.print("[yellow]⚠ 本次请求已取消，Leon 仍在运行。[/yellow]")
            return False
        except AgentCancelled as exc:
            self._record_cancelled_tool_audit(exc, result)
            self._stop_image_progress(ok=None)
            self.print("[yellow]⏹ 本次请求已取消，迟到结果已丢弃。[/yellow]")
            return False
        except Exception as exc:  # noqa: BLE001 - CLI should keep the session alive
            self._stop_image_progress(ok=False)
            self.print(f"[red]{self._format_request_error(exc)}[/red]")
            return False
        assert result is not None
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
        table = Table("#", "Session", "Messages", "Last user", "Updated", "Current")
        for index, item in enumerate(self.store.list_sessions(), start=1):
            messages = self.store.load_messages(item["id"], limit=8)
            last_user = next(
                (
                    message["content"]
                    for message in reversed(messages)
                    if message["role"] == "user"
                ),
                "",
            )
            preview = " ".join(last_user.split())
            if len(preview) > 36:
                preview = preview[:35] + "…"
            table.add_row(
                str(index),
                item["id"],
                str(item["message_count"]),
                preview or "-",
                str(item["updated_at"]),
                "*" if item["id"] == self.session_id else "",
            )
        self.print(table)

    def resume_session(self, value: str) -> None:
        reference = value.strip()
        if not reference:
            self.print("[yellow]用法：/resume <会话ID或 /history 序号>[/yellow]")
            self.show_history()
            return

        session_id = reference
        if reference.isdigit():
            index = int(reference)
            sessions = self.store.list_sessions(limit=max(10, index))
            if index < 1 or index > len(sessions):
                self.print(f"[red]历史序号不存在：{reference}[/red]")
                return
            session_id = sessions[index - 1]["id"]
        if not self.store.has_session(session_id):
            self.print(f"[red]会话不存在：{reference}[/red]")
            return
        if session_id == self.session_id:
            self.print(f"[dim]已经在会话 {session_id} 中。[/dim]")
            return

        self.session_id = session_id
        self.model_selection = self.store.get_model_selection(session_id)
        self._restore_last_exchange()
        self.agent = self._create_agent()
        ui = getattr(self, "ui", None)
        if ui is not None:
            ui.refresh_input_history()
        self.print(f"[green]已切换会话[/green] {session_id}")
        self._print_resume_context()

    def retry_last_message(self) -> bool:
        message = getattr(self, "_last_user_message", "") or ""
        if not message.strip():
            message = next(
                (
                    item["content"]
                    for item in reversed(self.store.load_messages(self.session_id))
                    if item["role"] == "user"
                ),
                "",
            )
        if not message.strip():
            self.print("[yellow]还没有可重试的请求。[/yellow]")
            return False
        self.print("[cyan]↻ 正在重试上一条请求（会追加一轮记录）…[/cyan]")
        return self.process(message)

    def _last_answer_text(self) -> str:
        answer = getattr(self, "_last_answer", "") or ""
        if answer.strip():
            return answer
        return next(
            (
                item["content"]
                for item in reversed(self.store.load_messages(self.session_id))
                if item["role"] == "assistant" and item["content"].strip()
            ),
            "",
        )

    def show_last_answer(self) -> bool:
        answer = self._last_answer_text()
        if not answer:
            self.print("[yellow]当前会话还没有可显示的回答。[/yellow]")
            return False
        self._print_answer(answer)
        return True

    def copy_last_answer(self, *, quiet: bool = False) -> bool:
        answer = self._last_answer_text()
        if not answer:
            if not quiet:
                self.print("[yellow]当前会话还没有可复制的回答。[/yellow]")
            return False
        executable = _copy_to_clipboard(answer)
        if executable is None:
            if not quiet:
                self.print(
                    "[yellow]未找到可用剪贴板命令；可先用 /last 查看，再从终端复制。[/yellow]"
                )
            return False
        if not quiet:
            self.print(f"[green]✓ 已复制上一条回答[/green] [dim]({Path(executable).name})[/dim]")
        return True

    def show_tools(self) -> None:
        registry = getattr(getattr(self.agent, "runtime", None), "tools", None)
        schemas = getattr(registry, "schemas", [])
        table = Table("Tool", "Description", "Mode")
        for schema in schemas:
            function = schema.get("function", {})
            table.add_row(
                str(function.get("name") or "-"),
                str(function.get("description") or "-"),
                "model tool",
            )
        if not schemas:
            self.print("[yellow]当前 Agent 没有可展示的工具。[/yellow]")
            return
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
        timeout = float(getattr(self, "llm_timeout_seconds", 0.0) or 0.0)
        timeout_label = "response=unlimited" if timeout <= 0 else f"timeout={timeout:g}s"
        body.append(
            f"{timeout_label} · "
            f"retries={getattr(self, 'llm_max_retries', 0)}\n"
        )
        body.append("图片后端   ", style="bold magenta")
        body.append(f"{getattr(getattr(self, 'config', None), 'backend_url', '-')}\n")
        body.append("联网搜索   ", style="bold green")
        body.append("已启用" if getattr(self, "search_service", None) else "未配置")
        self.print(Panel(body, title="当前运行状态", border_style="cyan"))

    def show_models(self) -> None:
        models = self._fetch_model_catalog()
        choices = list(models)
        if self.llm_model and self.llm_model not in choices:
            choices.append(self.llm_model)
        ui = getattr(self, "ui", None)
        if ui is not None and choices:
            ui.begin_model_picker(choices, current=self.llm_model)
            return
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
        if sys.stdin.isatty():
            choice = Prompt.ask("选择模型（输入序号或完整 ID，回车取消）", default="").strip()
            if choice:
                self.switch_model(choice)

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
        self._last_user_message = ""
        self._last_answer = ""
        self._last_image_url = None
        self.agent = self._create_agent()
        ui = getattr(self, "ui", None)
        if ui is not None:
            ui.refresh_input_history()
        self.print(f"[green]✓[/green] 新会话 [bold]{self.session_id}[/bold]")

    def handle_interactive_message(self, message: str) -> bool:
        """Handle one command or chat turn for either terminal frontend."""
        message = message.strip("\r\n")
        command = message.strip()
        if not command:
            return True
        command_parts = command.split(maxsplit=1)
        command_name = command_parts[0].casefold()
        command_argument = command_parts[1] if len(command_parts) > 1 else ""
        self._check_active_turn()
        if command_name in {"/exit", "/quit"} and not command_argument:
            return False
        if command_name == "/new" and not command_argument:
            self.new_session()
            self._check_active_turn()
            return True
        if command_name == "/history" and not command_argument:
            self.show_history()
            self._check_active_turn()
            return True
        if command_name == "/resume":
            self.resume_session(command_argument)
            self._check_active_turn()
            return True
        if command_name == "/retry" and not command_argument:
            self.retry_last_message()
            return True
        if command_name == "/last" and not command_argument:
            self.show_last_answer()
            self._check_active_turn()
            return True
        if command_name == "/copy" and not command_argument:
            self.copy_last_answer()
            self._check_active_turn()
            return True
        if command_name == "/open" and not command_argument:
            self.open_last_image()
            self._check_active_turn()
            return True
        if command_name == "/tools" and not command_argument:
            self.show_tools()
            self._check_active_turn()
            return True
        if command_name in {"/status", "/info"} and not command_argument:
            self.show_status()
            self._check_active_turn()
            return True
        if command_name == "/clear" and not command_argument:
            ui = getattr(self, "ui", None)
            if ui is not None:
                ui.clear_output()
                ui._set_status(TerminalChatUI._IDLE_STATUS)
            else:
                self.console.clear()
            self._check_active_turn()
            return True
        if command_name in {"/model", "/models"} and not command_argument:
            self.show_models()
            self._check_active_turn()
            return True
        if command_name in {"/model", "/models"}:
            self.switch_model(command_argument)
            self._check_active_turn()
            return True
        if command_name in {"/help", "/commands"} and not command_argument:
            self.print(
                Panel(
                    "[bold]/new[/bold] 新会话\n"
                    "[bold]/history[/bold] 会话列表\n"
                    "[bold]/resume <ID或序号>[/bold] 切换已有会话\n"
                    "[bold]/retry[/bold] 重试上一条请求（追加一轮记录）\n"
                    "[bold]/last[/bold] 重新显示上一条回答\n"
                    "[bold]/copy[/bold] 复制上一条回答到系统剪贴板\n"
                    "[bold]/open[/bold] 打开最近一张图片（无鼠标终端后备）\n"
                    "[bold]/tools[/bold] 查看当前 Agent 已注册工具\n"
                    "[bold]/status[/bold] 当前模型、provider、会话状态（/info）\n"
                    "[bold]/model[/bold] 打开模型选择器（/models）\n"
                    "[bold]/model <序号或模型ID>[/bold] 直接切换模型\n"
                    "[bold]/clear[/bold] 清空当前终端滚动区\n"
                    "[bold]/nsfw <描述>[/bold] 跳过 LLM，直接用 NSFW 模式生图\n"
                    "[bold]/exit[/bold] 退出\n\n"
                    "[dim]快捷键：Enter 发送 · Shift+Enter/Ctrl+J 换行\n"
                    "Esc 取消当前轮 · Ctrl+C 清输入；空闲时退出\n"
                    "Ctrl+D 退出 · Ctrl+L 清屏 · Ctrl+Insert 复制上一条回答\n"
                    "↑/↓ 历史 · Tab 命令补全\n"
                    "Ctrl+U 清空输入 · Ctrl+K 删除到行尾 · Ctrl+W 删除前一词\n"
                    "[/dim]\n\n"
                    "[dim]你也可以直接说：检查环境、生成图片、查询任务、查看最近图片。[/dim]",
                    title="Leon 命令",
                    border_style="dim",
                )
            )
            self._check_active_turn()
            return True
        if command_name == "/nsfw":
            self.process(command)
            return True
        if command.startswith("/"):
            self.print(
                f"[yellow]未知命令：{command.split(maxsplit=1)[0]}[/yellow] · "
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
                message = Prompt.ask(_legacy_prompt_markup(self.console)).strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]Leon Agent 已退出。[/dim]")
                return
            if not message:
                continue
            if not self.handle_interactive_message(message):
                return


def main() -> None:
    _configure_unicode_stdio()
    args = parse_args()
    try:
        app = LeonConsole(args)
    except Exception as exc:  # noqa: BLE001 - provide a readable startup failure
        Console().print(f"[red]Leon Agent 启动失败：{type(exc).__name__}: {exc}[/red]")
        raise SystemExit(1) from exc
    if args.once:
        raise SystemExit(0 if app.process(args.once) else 1)
    app.interactive()
    app.print_resume_hint()


if __name__ == "__main__":
    main()
