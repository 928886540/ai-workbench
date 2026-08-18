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
from collections import deque
from collections.abc import Sequence
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, unquote, urlsplit

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt
from rich.table import Column, Table
from rich.text import Text
from rich.tree import Tree
from workbench_core.agent import (
    AgentEvent,
    AgentResult,
    SpanRecord,
    ToolStep,
    TraceContext,
    TraceRecord,
    TraceRecorder,
)
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
from leon_agent.service import _wait_for_image_results
from leon_agent.session import SessionStore
from leon_agent.tools import create_leon_tools
from leon_agent.trace_store import SQLiteTraceStore

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
    from prompt_toolkit.cursor_shapes import CursorShape
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.filters import Condition, has_focus
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import (
        ConditionalContainer,
        Float,
        FloatContainer,
        HSplit,
        Layout,
        Window,
    )
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.layout.menus import CompletionsMenu
    from prompt_toolkit.mouse_events import MouseButton, MouseEventType
    from prompt_toolkit.styles import Style
    from prompt_toolkit.widgets import Frame, TextArea
except ModuleNotFoundError:  # pragma: no cover - legacy prompt fallback remains usable
    Application = None
    WordCompleter = None
    CursorShape = None
    Point = None
    Condition = None
    has_focus = None
    KeyBindings = None
    ConditionalContainer = None
    Float = None
    FloatContainer = None
    HSplit = None
    Layout = None
    Window = None
    FormattedTextControl = None
    Dimension = None
    CompletionsMenu = None
    InMemoryHistory = None
    MouseButton = None
    MouseEventType = None
    Style = None
    Frame = None
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
    "/trace": "查看最近一次本地 Trace",
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

_USER_PROMPT = "YOU  ❯ "
_ASSISTANT_PROMPT = "Leon ❯ "


@dataclass(frozen=True)
class RuntimeStatus:
    model: str
    provider: str
    session: str
    workspace: str
    tool_count: int
    memory_enabled: bool
    planning_enabled: bool
    trace_enabled: bool
    image_enabled: bool
    search_enabled: bool
    request_policy: str


@dataclass
class ModelPickerState:
    choices: tuple[str, ...]
    selected_index: int
    current: str
_WIN32_CTRL_PRESSED = 0x000C
_WIN32_ALT_PRESSED = 0x0003

_URL_PATTERN = re.compile(r"https?://[^\s<>{}\[\]()]+", re.IGNORECASE)
_IMAGE_SUFFIXES = (".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp")
_THINKING_BEAM_SPEED = 2.0
_THINKING_BEAM_TRAIL = 2.4
_THINKING_BEAM_GAP = 1.2
_CURSOR_BLINK_SECONDS = 0.53


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


def _launch_external_url(url: str) -> bool:
    """Hand a URL to the desktop without letting a shell split its query string."""

    if sys.platform == "win32":
        subprocess.Popen(
            ["explorer.exe", url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    return bool(webbrowser.open(url, new=2))


def _legacy_prompt_markup(console: Console) -> str:
    """Use the styled arrow in Unicode consoles and an ASCII prompt otherwise."""

    marker = "YOU  ❯" if _console_supports(console, "❯") else "YOU  >"
    return f"\n[bold #F5C26B]{marker}[/bold #F5C26B]"


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
    """Inline chat surface with explicit turn ownership and cancellation."""

    _MAX_BLOCKS = 480
    _RESUME_MESSAGE_LIMIT = 240
    _WORKED_SEPARATOR_PREFIX = "─ Worked for "
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
        self._model_picker: ModelPickerState | None = None
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
        self._queued_messages: deque[str] = deque()
        self._background_image_jobs: set[str] = set()
        self._animation_stop_event = threading.Event()
        self._animation_thread: threading.Thread | None = None
        self._cursor_visible = True
        self._cursor_blink_started_at = monotonic()

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
        )
        self.output_gap = Window(height=1)
        model_picker_active = Condition(lambda: self._model_picker is not None)
        input_kwargs = {
            "height": Dimension(min=1, max=6),
            "dont_extend_height": True,
            "get_line_prefix": self._composer_line_prefix,
            "multiline": True,
            "accept_handler": self._accept,
            "style": "class:composer.input",
            "prompt": [("class:composer.prompt", _USER_PROMPT)],
            "read_only": model_picker_active,
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
        # prompt_toolkit's Win32 output backend ignores blinking cursor shapes.
        # Toggle cursor visibility explicitly so Windows Terminal still blinks.
        self.input.window.always_hide_cursor = Condition(
            lambda: not self._cursor_visible
        )
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
        self.model_picker_control = FormattedTextControl(
            self._model_picker_fragments,
            focusable=False,
            show_cursor=False,
            get_cursor_position=self._model_picker_cursor_position,
        )
        self.model_picker_panel = ConditionalContainer(
            content=Frame(
                Window(
                    content=self.model_picker_control,
                    height=Dimension(min=1, max=8),
                    dont_extend_height=True,
                    always_hide_cursor=True,
                ),
                title="选择模型",
                style="class:model-picker.frame",
                height=Dimension(min=3, max=10),
            ),
            filter=model_picker_active,
        )
        key_bindings = KeyBindings()
        input_focused = has_focus(self.input)
        turn_busy = Condition(lambda: self.busy)

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

        @key_bindings.add("pageup")
        def _(event) -> None:  # noqa: ANN001, ARG001
            self._scroll_output_page(-1)

        @key_bindings.add("pagedown")
        def _(event) -> None:  # noqa: ANN001, ARG001
            self._scroll_output_page(1)

        body = HSplit(
            [
                self.output,
                self.output_gap,
                self.model_picker_panel,
                self.status,
                self.composer_top,
                self.input,
                self.composer_bottom,
                self.bottom_bar,
            ]
        )
        root = FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(
                        max_height=10,
                        scroll_offset=1,
                        display_arrows=True,
                        extra_filter=Condition(
                            lambda: self._model_picker is None
                        ),
                    ),
                )
            ],
        )
        tui_style = Style.from_dict(
            {
                "message.user": "#FFE0A3",
                "message.assistant": "#FFFFFF",
                "message.marker": "bold #00E5FF",
                "message.marker.old": "#666666",
                "message.tool": "#666666",
                "message.separator": "#666666",
                "message.link": "underline #73B8FF",
                "status": "#71869A",
                "status.running": "#666666",
                "status.success": "#00FF00",
                "status.error": "#FF66CC",
                "status.warning": "#F5C26B",
                "status.background": "#73B8FF",
                "status.pulse.hot": "bold #FFFFFF",
                "status.pulse.bright": "#DCEEFF",
                "status.pulse.mid": "#AFC4D6",
                "status.pulse.soft": "#899EAF",
                "status.pulse.dim": "#516579",
                "status.cancel": "#FF8FB1",
                "composer.line": "#006C78",
                "composer.prompt": "bold #F5C26B",
                "composer.input": "#FFE0A3",
                "composer.hint": "#71869A",
                "completion-menu": "bg:#202D38 #DCEEFF",
                "completion-menu.completion": "bg:#202D38 #DCEEFF",
                "completion-menu.completion.current": "bold bg:#2B3C49 #59E1F7",
                "completion-menu.meta.completion": "bg:#202D38 #71869A",
                "completion-menu.meta.completion.current": "bg:#2B3C49 #AFC4D6",
                "model-picker.frame": "bg:#202D38 #DCEEFF",
                "model-picker.item": "bg:#202D38 #DCEEFF",
                "model-picker.item.current": "bg:#202D38 #71869A",
                "model-picker.item.selected": "bold bg:#2B3C49 #59E1F7",
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
            # Inline mode keeps the normal terminal scrollback active. In a
            # full-screen alternate buffer Windows Terminal translates the
            # mouse wheel into Up/Down keys, which incorrectly moves through
            # composer history instead of scrolling the conversation.
            "full_screen": False,
            # Keep mouse reporting disabled so the host terminal owns drag
            # selection and Ctrl+Shift+C, just like a normal PowerShell tab.
            "mouse_support": False,
            "cursor": CursorShape.BLINKING_BEAM,
            "style": tui_style,
            # Continuous redraws reset the host terminal's cursor blink cycle.
            # Animated statuses get their own refresh loop while idle input
            # remains untouched.
            "refresh_interval": None,
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
    def _composer_line_prefix(line_number: int, wrap_count: int):
        if line_number == 0 and wrap_count == 0:
            return ""
        return " " * len(_USER_PROMPT)

    def _model_picker_cursor_position(self):
        with self.lock:
            picker = self._model_picker
            selected_index = picker.selected_index if picker is not None else 0
        return Point(x=0, y=selected_index)

    def _model_picker_fragments(self):
        with self.lock:
            picker = self._model_picker
            if picker is None:
                return []
            choices = picker.choices
            selected_index = picker.selected_index
            current = picker.current
        fragments = []
        for index, model in enumerate(choices):
            selected = index == selected_index
            if selected:
                style = "class:model-picker.item.selected"
                marker = "❯ "
            else:
                style = "class:model-picker.item"
                marker = "  "
            label = "跟随 provider 默认模型" if model == "default" else model
            suffix = "  (current)" if model == current else ""
            fragments.append((style, f"{marker}{index + 1}. {label}{suffix}"))
            if index < len(choices) - 1:
                fragments.append((style, "\n"))
        return fragments

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
            self._cursor_visible = True
            self._cursor_blink_started_at = monotonic()
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
            picker = self._model_picker
            if picker is not None:
                picker.selected_index = (picker.selected_index + direction) % len(
                    picker.choices
                )
                self.app.invalidate()
                return
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
        with self.lock:
            queued_count = len(self._queued_messages)
            background_count = len(self._background_image_jobs)
        if hint:
            fragments = list(hint)
            if queued_count:
                fragments.append(
                    ("class:bottom.meta", f"  ·  队列 {queued_count}")
                )
            return fragments
        model = getattr(self.owner, "llm_model", "-") or "-"
        provider = getattr(self.owner, "llm_provider_name", "-") or "-"
        session = getattr(self.owner, "session_id", "-") or "-"
        fragments = [
            ("class:bottom.model", model),
            ("class:bottom.meta", "  "),
            ("class:bottom.provider", provider),
            ("class:bottom.meta", "  "),
            ("class:bottom.path", str(Path.cwd())),
            ("class:bottom.meta", f"  ·  session {session[:8]}"),
        ]
        if queued_count:
            fragments.extend(
                [
                    ("class:bottom.meta", "  ·  "),
                    ("class:bottom.model", f"队列 {queued_count}"),
                ]
            )
        if background_count:
            fragments.extend(
                [
                    ("class:bottom.meta", "  ·  "),
                    ("class:message.link", f"后台生图 {background_count}"),
                ]
            )
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
            hint = "↑/↓ 选择 · Enter 确认 · Esc 返回"
        elif not text:
            return []
        elif busy:
            hint = "Enter 加入队列 · esc 取消当前轮"
        elif text.lstrip().startswith("/"):
            hint = "Tab 补全命令 · Enter 执行"
        else:
            hint = "Enter 发送 · Shift+Enter/Ctrl+J 换行 · Tab 补全"
        return [("class:composer.hint", hint)]

    def _status_height(self) -> Dimension:
        with self.lock:
            visible = (
                self.busy
                or bool(self._background_image_jobs)
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
            background_count = len(self._background_image_jobs)
        if busy and started_at is not None:
            now = monotonic()
            elapsed = self._format_elapsed(now - started_at)
            if status.startswith("⏹"):
                return [
                    ("class:status.cancel", f"◦ {status}"),
                    ("class:status", f" ({elapsed})"),
                ]
            fragments = [("class:status.running", "◈ THINK  ")]
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
        if background_count:
            return [
                (
                    "class:status.background",
                    f"◦ 后台生图 {background_count} 项 · 输入区可继续使用",
                )
            ]
        if status == self._IDLE_STATUS:
            return []
        style = "class:status.cancel" if status.startswith("⏹") else "class:status"
        return [(style, status)]

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
            background_count = len(self._background_image_jobs)
        if busy and started_at is not None:
            elapsed = self._format_elapsed(monotonic() - started_at)
            suffix = "" if status.startswith("⏹") else " • esc 取消"
            return f"◈ THINK  {status} ({elapsed}{suffix})"
        if background_count:
            return f"◦ 后台生图 {background_count} 项 · 输入区可继续使用"
        if status == self._IDLE_STATUS:
            return ""
        return status

    def add_background_image_jobs(self, job_ids: Sequence[str]) -> None:
        with self.lock:
            self._background_image_jobs.update(job_id for job_id in job_ids if job_id)
        self.app.invalidate()

    def remove_background_image_jobs(self, job_ids: Sequence[str]) -> None:
        with self.lock:
            self._background_image_jobs.difference_update(job_ids)
        self.app.invalidate()

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

    def _scroll_output_page(self, direction: int) -> None:
        rendered = self._rendered_blocks()
        line_count = max(1, rendered.count("\n"))
        render_info = getattr(self.output, "render_info", None)
        page_size = max(1, int(getattr(render_info, "window_height", 10)) - 1)
        current_scroll = max(0, int(getattr(self.output, "vertical_scroll", 0)))
        target = min(
            line_count - 1,
            max(0, current_scroll + (page_size if direction > 0 else -page_size)),
        )
        self.output.vertical_scroll = target
        self._follow_output = direction > 0 and target >= line_count - 1
        self.app.invalidate()

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
            opened = _launch_external_url(url)
        except (OSError, subprocess.SubprocessError, webbrowser.Error) as exc:
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
        rendered_lines = rendered.splitlines(keepends=True)
        assistant_lines = [
            index
            for index, line in enumerate(rendered_lines)
            if line.rstrip("\n").startswith(_ASSISTANT_PROMPT)
        ]
        latest_assistant_line = assistant_lines[-1] if assistant_lines else -1
        for line_index, line in enumerate(rendered_lines):
            line_without_newline = line.rstrip("\n")
            cursor = 0
            if not line_without_newline:
                base_style = ""
                continuation_style = ""
            elif line_without_newline.startswith("─"):
                base_style = "class:message.separator"
                continuation_style = ""
            elif line_without_newline.startswith(_USER_PROMPT):
                base_style = "class:message.user"
                continuation_style = base_style
                fragments.append(
                    ("class:composer.prompt", _USER_PROMPT, self._observe_output_mouse)
                )
                cursor = len(_USER_PROMPT)
            elif line_without_newline.startswith(_ASSISTANT_PROMPT):
                base_style = "class:message.assistant"
                continuation_style = base_style
                marker_style = (
                    "class:message.marker"
                    if line_index == latest_assistant_line
                    else "class:message.marker.old"
                )
                fragments.append(
                    (marker_style, _ASSISTANT_PROMPT, self._observe_output_mouse)
                )
                cursor = len(_ASSISTANT_PROMPT)
            elif line_without_newline.startswith(("● ", "◦ ")):
                base_style = "class:status.running"
                continuation_style = base_style
            elif line_without_newline.startswith(("◈ TOOL", "· TRACE")):
                base_style = "class:message.tool"
                continuation_style = base_style
            elif line_without_newline.startswith(("◆ DONE", "✓ ", "✅ ")):
                base_style = "class:status.success"
                continuation_style = base_style
            elif line_without_newline.startswith(("◇ ERROR", "✗ ", "❌ ", "💥 ")):
                base_style = "class:status.error"
                continuation_style = base_style
            elif line_without_newline.startswith("⏹ "):
                base_style = "class:status.cancel"
                continuation_style = base_style
            elif line_without_newline.startswith("⚠ "):
                base_style = "class:status.warning"
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
                fragments.extend(
                    [
                        ("[ZeroWidthEscape]", f"\x1b]8;;{url}\x1b\\"),
                        ("class:message.link", "↗ 打开图片"),
                        ("[ZeroWidthEscape]", "\x1b]8;;\x1b\\"),
                    ]
                )
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
        output = getattr(self.app, "output", None)
        self._start_animation_refresh()
        try:
            if output is not None and hasattr(output, "set_title"):
                output.set_title("✦ Leon Agent")
            self.owner._print_startup()
            if getattr(self.owner, "_resumed_session", False):
                self.owner._print_resume_context()
            self.app.run()
        finally:
            self._stop_animation_refresh()
            self._shutdown_worker()
            if output is not None and hasattr(output, "reset_title"):
                output.reset_title()
            self.owner.ui = None

    def _start_animation_refresh(self) -> None:
        thread = self._animation_thread
        if thread is not None and thread.is_alive():
            return
        self._animation_stop_event.clear()
        thread = threading.Thread(
            target=self._animation_refresh_loop,
            name="leon-tui-animation",
            daemon=True,
        )
        self._animation_thread = thread
        thread.start()

    def _animation_refresh_loop(self) -> None:
        while not self._animation_stop_event.wait(1 / 12):
            now = monotonic()
            with self.lock:
                animated = self._status_animated
                blink_due = (
                    now - self._cursor_blink_started_at >= _CURSOR_BLINK_SECONDS
                )
                if blink_due:
                    self._cursor_visible = not self._cursor_visible
                    self._cursor_blink_started_at = now
            if animated or blink_due:
                self.app.invalidate()

    def _stop_animation_refresh(self) -> None:
        self._animation_stop_event.set()
        thread = self._animation_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        self._animation_thread = None

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

    def _message_body_width(self, prompt: str) -> int:
        return max(1, self._render_width() - len(prompt))

    @staticmethod
    def _render_plain_lines(text: str, width: int) -> list[str]:
        buffer = io.StringIO()
        render_console = Console(
            file=buffer,
            width=max(1, width),
            color_system=None,
            force_terminal=False,
        )
        render_console.print(Text(text), highlight=False, overflow="fold")
        return buffer.getvalue().rstrip("\n").splitlines()

    def _separator_width(self) -> int:
        render_info = getattr(self.output, "render_info", None)
        width = getattr(render_info, "window_width", 0)
        if not width:
            width = shutil.get_terminal_size(fallback=(100, 24)).columns
        return max(1, int(width))

    def write_answer(self, answer: str) -> None:
        lines, urls = _render_answer_lines(
            answer,
            self._message_body_width(_ASSISTANT_PROMPT),
        )
        lines = _normalise_answer_lines(lines)
        lines.extend(
            url + _image_link_suffix(index, len(urls))
            for index, url in enumerate(urls, start=1)
        )
        if not lines:
            lines = ["（空回答）"]
        parts = [
            (_ASSISTANT_PROMPT if index == 0 else " " * len(_ASSISTANT_PROMPT)) + line
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

    def write_turn_separator(self, elapsed_seconds: float | None = None) -> None:
        width = self._separator_width()
        if elapsed_seconds is None:
            separator = "─" * width
        else:
            label = f"{self._WORKED_SEPARATOR_PREFIX}{self._format_elapsed(elapsed_seconds)} "
            separator = label + "─" * max(1, width - len(label))
        self.write_plain(separator)

    def _retire_worked_separator(self) -> None:
        with self.lock:
            for index in range(len(self.blocks) - 1, -1, -1):
                if self.blocks[index].startswith(self._WORKED_SEPARATOR_PREFIX):
                    self.blocks[index] = "─" * self._separator_width()
                    self._follow_output = True
                    break
            else:
                return
        self.app.invalidate()

    def clear_output(self) -> None:
        with self.lock:
            self.blocks.clear()
            self._latest_image_url = None
            self._follow_output = True
        self.app.invalidate()

    def write_user_message(self, message: str) -> None:
        lines = self._render_plain_lines(
            message,
            self._message_body_width(_USER_PROMPT),
        ) or [""]
        body = "\n".join(
            (_USER_PROMPT if index == 0 else " " * len(_USER_PROMPT)) + line
            for index, line in enumerate(lines)
        )
        self.write_plain(body)

    def open_latest_image(self) -> None:
        url = self._latest_image_url
        if not url:
            self.write_plain("[没有可打开的图片]")
            return
        self._open_url(url)

    def begin_model_picker(self, models: Sequence[str], *, current: str) -> None:
        choices = tuple(dict.fromkeys([*(model for model in models if model), "default"]))
        if not choices:
            return
        selected_index = choices.index(current) if current in choices else 0
        self.input.buffer.text = ""
        with self.lock:
            self._model_picker = ModelPickerState(
                choices=choices,
                selected_index=selected_index,
                current=current,
            )
        self.app.invalidate()

    def cancel_model_picker(self, *, silent: bool = False) -> None:  # noqa: ARG002
        with self.lock:
            self._model_picker = None
        self._set_status(self._IDLE_STATUS)

    def _accept_model_choice(
        self,
        buffer,
        candidate: str | None = None,
    ) -> bool:  # noqa: ANN001
        with self.lock:
            picker = self._model_picker
            selected = (
                picker.choices[picker.selected_index]
                if picker is not None
                else None
            )
        resolved = candidate or selected
        if not resolved:
            return False
        with self.lock:
            self._model_picker = None
        buffer.text = ""
        self.owner.switch_model(resolved)
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
                self._queued_messages.clear()
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
            self._accept_model_choice(event.current_buffer)
            return
        completion_state = getattr(event.current_buffer, "complete_state", None)
        if completion_state is not None and completion_state.completions:
            completion = (
                completion_state.current_completion
                or completion_state.completions[0]
            )
            previous_text = event.current_buffer.text
            event.current_buffer.apply_completion(completion)
            if event.current_buffer.text != previous_text:
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
            buffer.text = ""
            with self.lock:
                self._history_entries.append(message)
                self._history_index = len(self._history_entries)
                self._history_draft = ""
                self._queued_messages.append(message)
                queued_count = len(self._queued_messages)
            self.write_plain(
                f"◦ 已加入消息队列 · 当前轮结束后自动发送（待发送 {queued_count}）"
            )
            return False
        if buffer.text != message:
            # Keep the history entry identical to the text sent to the agent;
            # a final Shift+Enter should not leave a phantom blank line.
            buffer.text = message
        with self.lock:
            self._history_entries.append(message)
            self._history_index = len(self._history_entries)
            self._history_draft = ""
        self._launch_turn(message)
        return False

    def _launch_turn(self, message: str) -> None:
        self._retire_worked_separator()
        self.write_user_message(message)
        if message.casefold() in {"/exit", "/quit"}:
            self.app.exit()
            return

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

    def _run_message(
        self,
        message: str,
        generation: int,
        cancel_event: threading.Event,
    ) -> None:
        token = _ACTIVE_TURN.set((generation, cancel_event))
        completed = False
        try:
            with cancellation_scope(cancel_event):
                keep_running = self.owner.handle_interactive_message(message)
                completed = bool(keep_running)
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
            queued_message: str | None = None
            started_at: float | None = None
            with self.lock:
                current = (
                    self._generation == generation
                    and self._active_cancel_event is cancel_event
                )
                if current:
                    should_exit = self._exit_requested
                    started_at = self._started_at
                    self.busy = False
                    self._active_cancel_event = None
                    self._active_thread = None
                    self._started_at = None
                    if not should_exit and self._queued_messages:
                        queued_message = self._queued_messages.popleft()
            if current:
                if should_exit:
                    self._set_status("👋 正在退出")
                    self.app.exit()
                else:
                    if completed and started_at is not None:
                        self.write_turn_separator(monotonic() - started_at)
                    else:
                        self.write_turn_separator()
                    if queued_message is not None:
                        self._launch_turn(queued_message)
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
        self.trace_store = SQLiteTraceStore(self.config.session_db)
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
        self._background_image_lock = threading.RLock()
        self._tracked_image_jobs: set[str] = set()
        self._background_image_threads: set[threading.Thread] = set()
        # One-shot invocations have no long-lived UI/process to receive a later
        # completion notification, so they keep the synchronous result contract.
        self.background_image_tracking = not bool(args.once)
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
        messages = self.store.load_messages(
            self.session_id,
            limit=TerminalChatUI._RESUME_MESSAGE_LIMIT,
        )
        ui = getattr(self, "ui", None)
        for message in messages:
            content = str(message["content"])
            if message["role"] == "user":
                if ui is not None:
                    ui.write_user_message(content)
                else:
                    self.print(Text(f"{_USER_PROMPT}{content}", style="#FFE0A3"))
            elif ui is not None:
                ui.write_answer(content)
                ui.write_turn_separator()
            else:
                self._print_answer(content)
        # Rendering older answers must not change /retry, /last, /copy or
        # /open away from the actual latest exchange.
        self._restore_last_exchange()

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

    def _get_trace_store(self) -> SQLiteTraceStore:
        trace_store = getattr(self, "trace_store", None)
        if trace_store is None:
            trace_store = SQLiteTraceStore(self.store.path)
            self.trace_store = trace_store
        return trace_store

    def _new_trace_context(self, *, entrypoint: str) -> TraceContext:
        return TraceContext.create(
            session_id=self.session_id,
            entrypoint=entrypoint,  # type: ignore[arg-type]
        )

    @staticmethod
    def _trace_hint(trace_context: TraceContext | None) -> str:
        if trace_context is None:
            return ""
        return f" (trace {trace_context.trace_id[:8]})"

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
        trace_context: TraceContext | None = None,
    ) -> None:
        """Persist only completed, already-projected tool steps from a cancelled turn."""
        partial_result = getattr(exc, "partial_result", None)
        if not isinstance(partial_result, AgentResult) or not partial_result.steps:
            partial_result = fallback_result
        if not isinstance(partial_result, AgentResult) or not partial_result.steps:
            return
        audit_result = AgentResult(
            answer="",
            steps=list(partial_result.steps),
            messages=[],
            trace_id=partial_result.trace_id or (
                trace_context.trace_id if trace_context is not None else None
            ),
            turn_id=partial_result.turn_id or (
                trace_context.turn_id if trace_context is not None else None
            ),
        )
        with self._commit_context():
            self.store.record_result(self.session_id, audit_result)

    def _on_generation_submitted(self, submission: dict[str, object]) -> None:
        """Track submitted image jobs without keeping the CLI turn busy."""
        jobs = [
            item
            for item in submission.get("jobs", [])
            if isinstance(item, dict) and item.get("job_id")
        ]
        job_ids = [str(item["job_id"]) for item in jobs]
        if not job_ids:
            return

        lock = getattr(self, "_background_image_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._background_image_lock = lock
        with lock:
            tracked = getattr(self, "_tracked_image_jobs", set())
            fresh_job_ids = [job_id for job_id in job_ids if job_id not in tracked]
            if not fresh_job_ids:
                return
            tracked.update(fresh_job_ids)
            self._tracked_image_jobs = tracked

        session_id = self.session_id
        image_client = self.image_client
        tracked_submission = {
            **submission,
            "jobs": [item for item in jobs if str(item["job_id"]) in fresh_job_ids],
        }
        ui = getattr(self, "ui", None)
        if ui is not None and hasattr(ui, "add_background_image_jobs"):
            ui.add_background_image_jobs(fresh_job_ids)
        elif ui is None:
            self.print(
                f"[cyan]◦[/cyan] 已提交 {len(fresh_job_ids)} 张图片任务，后台生成中"
            )

        thread = threading.Thread(
            target=self._track_image_submission,
            args=(session_id, image_client, tracked_submission, fresh_job_ids),
            name=f"leon-image-{fresh_job_ids[0][:8]}",
            daemon=True,
        )
        with lock:
            threads = getattr(self, "_background_image_threads", set())
            threads.add(thread)
            self._background_image_threads = threads
        try:
            thread.start()
        except Exception:
            with lock:
                self._tracked_image_jobs.difference_update(fresh_job_ids)
                self._background_image_threads.discard(thread)
            if ui is not None and hasattr(ui, "remove_background_image_jobs"):
                ui.remove_background_image_jobs(fresh_job_ids)
            raise

    def _track_image_submission(
        self,
        session_id: str,
        image_client: LeonImageClient,
        submission: dict[str, object],
        job_ids: list[str],
    ) -> None:
        # Give the foreground tool result a short head start so its audit row is
        # persisted before an unusually fast backend completion notification.
        threading.Event().wait(0.35)
        try:
            result = _wait_for_image_results(
                image_client,
                chat_id=f"leon-agent:{session_id}",
                submission=submission,
                poll_interval_seconds=1.0,
            )
        except Exception as exc:  # noqa: BLE001 - background tracking must not kill the TUI
            result = {
                **submission,
                "ok": False,
                "images": [],
                "error": f"后台状态查询失败：{type(exc).__name__}: {exc}",
            }
        finally:
            lock = getattr(self, "_background_image_lock", None)
            if lock is not None:
                with lock:
                    self._tracked_image_jobs.difference_update(job_ids)
                    self._background_image_threads.discard(threading.current_thread())

        self._finish_image_submission(session_id, job_ids, result)

    def _finish_image_submission(
        self,
        session_id: str,
        job_ids: Sequence[str],
        result: dict[str, object],
    ) -> None:
        ui = getattr(self, "ui", None)
        if ui is not None and hasattr(ui, "remove_background_image_jobs"):
            ui.remove_background_image_jobs(job_ids)

        images = [
            str(item["image_url"])
            for item in result.get("images", [])
            if isinstance(item, dict) and item.get("image_url")
        ]
        if result.get("ok") and images:
            answer = f"{len(images)} 张图片生成好了。\n\n" + "\n".join(
                f"- {url}" for url in images
            )
        else:
            detail = str(result.get("error") or "图片任务结束，但没有返回可用地址")
            answer = f"后台生图未完成：{detail}"

        try:
            self.store.add_message(session_id, "assistant", answer)
        except Exception as exc:  # noqa: BLE001 - still show the completed image to the user
            if ui is not None:
                ui.write_plain(f"⚠ 后台图片结果持久化失败：{type(exc).__name__}")

        if session_id == self.session_id:
            self._print_answer(answer)
            return
        notice = f"会话 {session_id[:8]} 的后台生图通知\n{answer}"
        if ui is not None:
            ui.write_answer(notice)
        else:
            self.print(Text(notice, style="#DCEEFF"))

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
            fallback_api_key=(
                self.config.tavily_fallback_api_key.get_secret_value()
                if self.config.tavily_fallback_api_key
                else None
            ),
            fallback_base_url=self.config.tavily_fallback_base_url,
        )
        self.file_service = create_file_search_service(self.config.file_roots)
        self.file_write_service = create_file_write_service(self.config.file_roots)
        self.memory_service = MemoryService(
            self.memory_store,
            session_id=self.session_id,
        )
        background_images = bool(getattr(self, "background_image_tracking", True))
        generation_callback = (
            self._on_generation_submitted if background_images else None
        )
        self.direct_tools = create_leon_tools(
            self.image_client,
            session_id=self.session_id,
            default_mode_ids=self.config.default_mode_ids,
            wait_for_image_completion=not background_images,
            on_generation_submitted=generation_callback,
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
            wait_for_image_completion=not background_images,
            on_generation_submitted=generation_callback,
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
        return max(1, int(width))

    def _runtime_status(self) -> RuntimeStatus:
        runtime = getattr(getattr(self, "agent", None), "runtime", None)
        registry = getattr(runtime, "tools", None)
        tool_names = getattr(registry, "names", ())
        timeout = float(getattr(self, "llm_timeout_seconds", 0.0) or 0.0)
        timeout_label = "unlimited" if timeout <= 0 else f"{timeout:g}s"
        retries = int(getattr(self, "llm_max_retries", 0) or 0)
        return RuntimeStatus(
            model=getattr(self, "llm_model", "-") or "-",
            provider=(
                getattr(self, "llm_provider_name", "")
                or getattr(self, "llm_profile", "-")
                or "-"
            ),
            session=str(getattr(self, "session_id", "-") or "-"),
            workspace=str(Path.cwd()),
            tool_count=len(tool_names),
            memory_enabled=getattr(self, "memory_service", None) is not None,
            planning_enabled=getattr(getattr(self, "agent", None), "planning_service", None)
            is not None,
            trace_enabled=getattr(self, "trace_store", None) is not None,
            image_enabled=bool(getattr(getattr(self, "config", None), "backend_url", "")),
            search_enabled=getattr(self, "search_service", None) is not None,
            request_policy=f"response={timeout_label} · retry={retries}",
        )

    @staticmethod
    def _status_field(label: str, value: str, *, width: int) -> Text:
        field = Text()
        field.append(label, style="#666666")
        field.append(LeonConsole._clip_startup(value, max(1, width - len(label))), style="#FFFFFF")
        return field

    def _telemetry_table(self, status: RuntimeStatus, *, session_label: str) -> Table:
        table = Table(
            Column(width=40, no_wrap=True, overflow="ellipsis"),
            Column(width=19, no_wrap=True, overflow="ellipsis"),
            box=box.SQUARE,
            border_style="#666666",
            show_header=False,
            show_footer=False,
            padding=(0, 1),
            width=66,
        )
        table.add_row(
            self._status_field("MODEL      ", status.model, width=40),
            self._status_field("SESSION  ", session_label, width=19),
        )
        table.add_row(
            self._status_field("WORKSPACE  ", status.workspace, width=40),
            self._status_field("TOOLS    ", f"{status.tool_count} active", width=19),
        )
        table.add_row(
            "",
            self._status_field(
                "MEMORY   ", "online" if status.memory_enabled else "offline", width=19
            ),
        )
        table.add_row(
            self._status_field("SYSTEM     ", "Plan · Search · Create · Sync", width=40),
            self._status_field(
                "TRACE    ", "enabled" if status.trace_enabled else "disabled", width=19
            ),
        )
        return table

    @staticmethod
    def _brand_panel() -> Panel:
        inner_width = 64
        logo = Text()
        logo.append(" " * inner_width + "\n")
        for line in (
            "    ██       ████████ ████████ ███    ██",
            "    ██       ██       ██    ██ ████   ██      A G E N T",
            "    ██       ██████   ██    ██ ██ ██  ██",
            "    ██       ██       ██    ██ ██  ██ ██",
            "    ████████ ████████ ████████ ██   ████",
        ):
            logo.append(f"{line.ljust(inner_width)}\n", style="bold #00E5FF")
        logo.append(" " * inner_width + "\n")
        logo.append("Your workspace, augmented.".center(inner_width), style="#FFFFFF")
        return Panel(
            logo,
            box=box.SQUARE,
            border_style="#00E5FF",
            padding=(0, 0),
            width=66,
        )

    @staticmethod
    def _capability_strip() -> Text:
        strip = Text("\n  ")
        for index, (icon, label) in enumerate(
            (("◈", "Plan"), ("⌕", "Search"), ("▤", "Create"), ("◉", "Remember"))
        ):
            if index:
                strip.append("    ")
            strip.append(f"{icon} ", style="#00E5FF")
            strip.append(label, style="#666666")
        return strip

    def _compact_startup(self, status: RuntimeStatus, width: int) -> None:
        session_label = status.session[:8] if getattr(self, "_resumed_session", False) else "new"
        lines = Text()
        lines.append("Plan · Search · Create · Remember\n", style="#666666")
        lines.append("model      ", style="#666666")
        lines.append(f"{self._clip_startup(status.model, max(1, width - 14))}\n", style="#FFFFFF")
        lines.append("session    ", style="#666666")
        lines.append(f"{session_label}\n", style="#FFFFFF")
        lines.append("tools      ", style="#666666")
        lines.append(f"{status.tool_count} active", style="#FFFFFF")
        self.print(
            Panel(
                lines,
                title="LEON AGENT",
                title_align="left",
                box=box.SQUARE,
                border_style="#00E5FF",
                padding=(0, 1),
                width=width,
            )
        )

    def _minimal_startup(self, status: RuntimeStatus, width: int) -> None:
        session_label = status.session[:8] if getattr(self, "_resumed_session", False) else "new"
        summary = self._clip_startup(
            f"{status.model} · {session_label} · {status.tool_count} tools",
            width,
        )
        title = Text("LEON AGENT\n", style="bold #00E5FF")
        title.append(summary, style="#666666")
        self.print(title)

    def _print_startup(self) -> None:
        width = self._startup_width()
        status = self._runtime_status()
        resumed = bool(getattr(self, "_resumed_session", False))
        if width >= 66 and not resumed:
            self.print(
                Group(
                    self._brand_panel(),
                    self._telemetry_table(status, session_label="new"),
                    self._capability_strip(),
                )
            )
            return
        if width >= 40:
            self._compact_startup(status, min(66, width))
            return
        self._minimal_startup(status, width)

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
            ui._set_status("正在提交生图任务", animate=True)
            return
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}"),
            TimeElapsedColumn(),
            console=self.console,
        )
        self._progress.start()
        self._progress_task_id = self._progress.add_task("正在提交生图任务…", total=None)

    def _stop_image_progress(self, *, ok: bool | None = None) -> None:
        active = bool(getattr(self, "_image_progress_active", False))
        ui = getattr(self, "ui", None)
        if not active and getattr(self, "_progress", None) is None:
            return
        self._image_progress_active = False
        if ui is not None:
            if ok is not None:
                ui.write_plain("◆ DONE   图片生成完成" if ok else "◇ ERROR  图片生成失败")
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
            self.print(f"[grey50]◈ TOOL   [/grey50][white]{event.tool_name}[/white]")
        elif event.kind == "tool_finished":
            ok = bool(event.result and event.result.get("ok"))
            if event.tool_name == "generate_images":
                background = bool(
                    event.result
                    and event.result.get("waited_for_completion") is False
                )
                self._stop_image_progress(ok=None if background else ok)
                if background:
                    self.print("[green]◆ DONE   [/green][grey50]generate_images 已提交[/grey50]")
                    return
            if ok:
                self.print(f"[green]◆ DONE   [/green][grey50]{event.tool_name} 完成[/grey50]")
            else:
                self.print(f"[#FF66CC]◇ ERROR  [/#FF66CC][grey50]{event.tool_name} 失败[/grey50]")

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
        marker = (
            _ASSISTANT_PROMPT
            if _console_supports(self.console, "❯")
            else "Leon > "
        )
        width = max(
            1,
            min(100, int(getattr(self.console, "width", 100)) - 2 - len(marker)),
        )
        lines, urls = _render_answer_lines(answer, width)
        lines = _normalise_answer_lines(lines)
        link_label = "↗ 打开图片" if _console_supports(self.console, "↗") else "打开图片"
        output = Text()
        for index, line in enumerate(lines):
            if index:
                output.append("\n" + " " * len(marker))
            else:
                output.append(marker, style="bold #00E5FF")
            output.append(line)
        for index, url in enumerate(urls, start=1):
            output.append("\n" + " " * len(marker))
            output.append(link_label, style=f"underline #73B8FF link {url}")
            output.append(_image_link_suffix(index, len(urls)), style="dim")
        if not lines and not urls:
            output.append(marker, style="bold #00E5FF")
            output.append("（空回答）")
        # Let the terminal perform any visual wrapping. Rich must not inject a
        # newline into the hidden OSC 8 target carried by the short link.
        self.print(output, soft_wrap=True)

    def open_last_image(self) -> bool:
        url = getattr(self, "_last_image_url", None)
        if not url:
            self.print("[yellow]当前会话还没有可打开的图片。[/yellow]")
            return False
        try:
            opened = _launch_external_url(url)
        except (OSError, subprocess.SubprocessError, webbrowser.Error) as exc:
            self.print(f"[red]打开图片失败：{type(exc).__name__}: {exc}[/red]")
            return False
        if not opened:
            self.print("[yellow]系统没有可用的浏览器打开图片链接。[/yellow]")
            return False
        self.print("[green]↗ 已交给系统浏览器[/green]")
        return True

    def _start_llm_request(self) -> None:
        """Show feedback before the provider call, including in the legacy REPL."""
        self._check_active_turn()
        ui = getattr(self, "ui", None)
        if ui is not None:
            ui._set_status("正在思考中", animate=True)
            return
        marker = "◈ THINK  " if _console_supports(self.console, "◈") else "THINK  "
        self.print(f"[grey50]{marker}[/grey50]正在思考中")

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
        trace_context: TraceContext | None = None
        try:
            self._check_active_turn()
            if stripped.casefold() == "/nsfw" or stripped.casefold().startswith("/nsfw "):
                trace_context = self._new_trace_context(entrypoint="direct")
                return self._process_nsfw(stripped, trace_context=trace_context)
            self._ensure_current_provider()
            self._check_active_turn()
            history = self.store.load_messages(self.session_id)
            trace_context = self._new_trace_context(entrypoint="cli")
            self._start_llm_request()
            result = self.agent.run(
                message,
                history=history,
                trace_context=trace_context,
                trace_sink=self._get_trace_store(),
            )
            result.trace_id = result.trace_id or trace_context.trace_id
            result.turn_id = result.turn_id or trace_context.turn_id
            self._check_active_turn()
            result.answer = _normalise_unicode_text(result.answer)
            with self._commit_context():
                self._check_active_turn()
                self.store.add_message(
                    self.session_id,
                    "user",
                    message,
                    turn_id=result.turn_id,
                )
                self.store.record_result(self.session_id, result)
                self.store.add_message(
                    self.session_id,
                    "assistant",
                    result.answer,
                    turn_id=result.turn_id,
                )
        except KeyboardInterrupt:
            self._stop_image_progress(ok=False)
            hint = self._trace_hint(trace_context)
            self.print(f"[yellow]⚠ 本次请求已取消，Leon 仍在运行。{hint}[/yellow]")
            return False
        except AgentCancelled as exc:
            self._record_cancelled_tool_audit(exc, result, trace_context)
            self._stop_image_progress(ok=None)
            hint = self._trace_hint(trace_context)
            self.print(f"[yellow]⏹ 本次请求已取消，迟到结果已丢弃。{hint}[/yellow]")
            return False
        except Exception as exc:  # noqa: BLE001 - CLI should keep the session alive
            self._stop_image_progress(ok=False)
            hint = self._trace_hint(trace_context)
            self.print(f"[red]{self._format_request_error(exc)}{hint}[/red]")
            return False
        assert result is not None
        self._print_answer(result.answer)
        return True

    def _process_nsfw(
        self,
        message: str,
        *,
        trace_context: TraceContext,
    ) -> bool:
        trace = TraceRecorder(trace_context, self._get_trace_store())
        try:
            self._check_active_turn()
            mode_result = self.image_client.list_modes()
            self._check_active_turn()
            modes = mode_result.get("modes", [])
            command = parse_nsfw_command(message, modes)
        except AgentCancelled:
            trace.finish_trace(status="cancelled", outcome="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - invalid command should not exit the REPL
            trace.finish_trace(
                status="error",
                outcome="failed",
                error_type=type(exc).__name__,
            )
            self.print(f"[red]{exc}{self._trace_hint(trace_context)}[/red]")
            if "modes" in locals():
                self.print(Markdown(format_mode_catalog(modes)))
            return False
        if command is None:
            trace.finish_trace(status="ok", outcome="direct_answer")
            self.print(Markdown(format_mode_catalog(modes)))
            return True
        arguments = {
            "source_text": command.source_text,
            "workflow_ids": [command.workflow_id],
            "batch_count": 1,
        }
        tool_span_id = trace.start_span(
            "tool",
            "tool.call",
            tool_name="generate_images",
        )
        self._start_image_progress()
        try:
            self._check_active_turn()
            result = self.direct_tools.execute("generate_images", arguments)
        except KeyboardInterrupt:
            trace.finish_span(tool_span_id, status="cancelled")
            trace.finish_trace(status="cancelled", outcome="cancelled")
            self._stop_image_progress(ok=False)
            self.print(
                "[yellow]⚠ 本次生图已取消，Leon 仍在运行。"
                f"{self._trace_hint(trace_context)}[/yellow]"
            )
            return False
        except AgentCancelled:
            trace.finish_span(tool_span_id, status="cancelled")
            trace.finish_trace(status="cancelled", outcome="cancelled")
            self._stop_image_progress(ok=None)
            raise
        except Exception as exc:  # noqa: BLE001 - image failure should not exit the REPL
            trace.finish_span(
                tool_span_id,
                status="error",
                error_type=type(exc).__name__,
            )
            trace.finish_trace(
                status="error",
                outcome="failed",
                error_type=type(exc).__name__,
            )
            self._stop_image_progress(ok=False)
            self.print(
                f"[red]直达生图失败：{type(exc).__name__}: {exc}"
                f"{self._trace_hint(trace_context)}[/red]"
            )
            return False
        ok = bool(result.get("ok"))
        trace.finish_span(
            tool_span_id,
            status="ok" if ok else "error",
            error_type=None if ok else "tool_error",
        )
        background = result.get("waited_for_completion") is False
        self._stop_image_progress(ok=None if background else ok)
        if not ok:
            trace.finish_trace(status="ok", outcome="direct_answer")
            self.print(
                f"[red]直达生图失败：{result.get('error') or '未知错误'}"
                f"{self._trace_hint(trace_context)}[/red]"
            )
            return False
        images = [
            item.get("image_url")
            for item in result.get("images", [])
            if isinstance(item, dict) and item.get("image_url")
        ]
        if background:
            answer = (
                f"已使用 {command.mode_name} 模式提交 1 张图片任务，正在后台生成；"
                "你可以继续聊天，完成后会自动显示。"
            )
        else:
            answer = f"{command.mode_name}模式的图片生成好了。"
            if images:
                answer += "\n\n" + "\n".join(f"- {url}" for url in images)
        audit_arguments = getattr(self.direct_tools, "audit_arguments", None)
        audit_result = getattr(self.direct_tools, "audit_result", None)
        safe_arguments = (
            audit_arguments("generate_images", arguments)
            if callable(audit_arguments)
            else arguments
        )
        safe_result = (
            audit_result("generate_images", result)
            if callable(audit_result)
            else result
        )
        agent_result = AgentResult(
            answer=answer,
            steps=[
                ToolStep(
                    "generate_images",
                    safe_arguments,
                    safe_result,
                    trace_id=trace_context.trace_id,
                    span_id=tool_span_id,
                )
            ],
            trace_id=trace_context.trace_id,
            turn_id=trace_context.turn_id,
        )
        try:
            self._check_active_turn()
            with self._commit_context():
                self._check_active_turn()
                self.store.add_message(
                    self.session_id,
                    "user",
                    message,
                    turn_id=trace_context.turn_id,
                )
                self.store.record_result(self.session_id, agent_result)
                self.store.add_message(
                    self.session_id,
                    "assistant",
                    answer,
                    turn_id=trace_context.turn_id,
                )
        except AgentCancelled:
            trace.finish_trace(status="cancelled", outcome="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - keep the terminal app alive
            trace.finish_trace(
                status="error",
                outcome="failed",
                error_type=type(exc).__name__,
            )
            self.print(
                f"[red]直达生图结果保存失败：{type(exc).__name__}"
                f"{self._trace_hint(trace_context)}[/red]"
            )
            return False
        trace.finish_trace(status="ok", outcome="direct_answer")
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
            ui.clear_output()
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

    @staticmethod
    def _trace_duration(duration_ms: float | None) -> str:
        return "n/a" if duration_ms is None else f"{duration_ms:.1f} ms"

    @classmethod
    def _trace_span_label(cls, span: SpanRecord) -> Text:
        shown_status = "incomplete" if span.status == "running" else span.status
        style = {
            "ok": "green",
            "error": "red",
            "cancelled": "yellow",
            "incomplete": "yellow",
        }.get(shown_status, "dim")
        label = Text()
        label.append(f"{span.kind}:{span.name}")
        label.append(f"  {shown_status}", style=style)
        label.append(f"  {cls._trace_duration(span.duration_ms)}", style="dim")
        if span.tool_name:
            label.append(f"  tool={span.tool_name}", style="cyan")
        if span.model:
            label.append(f"  model={span.model}", style="cyan")
        if span.error_type:
            label.append(f"  error={span.error_type}", style="red")
        return label

    @classmethod
    def _trace_tree(cls, trace: TraceRecord, spans: list[SpanRecord]) -> Tree:
        by_id = {span.span_id: span for span in spans}
        children: dict[str | None, list[SpanRecord]] = {}
        for span in spans:
            children.setdefault(span.parent_span_id, []).append(span)
        for group in children.values():
            group.sort(key=lambda item: item.sequence_no)

        root_span = by_id.get(trace.root_span_id)
        root = Tree(
            cls._trace_span_label(root_span)
            if root_span is not None
            else Text("agent:agent.run  incomplete", style="yellow")
        )
        visited = {trace.root_span_id}

        def append_children(parent_id: str, branch: Tree) -> None:
            for span in children.get(parent_id, []):
                if span.span_id in visited:
                    continue
                visited.add(span.span_id)
                child = branch.add(cls._trace_span_label(span))
                append_children(span.span_id, child)

        append_children(trace.root_span_id, root)
        for span in sorted(spans, key=lambda item: item.sequence_no):
            if span.span_id not in visited:
                visited.add(span.span_id)
                root.add(cls._trace_span_label(span))
        return root

    def show_trace(self) -> bool:
        try:
            traces = self._get_trace_store().list_traces(self.session_id, limit=1)
            if not traces:
                self.print("[yellow]当前会话还没有 Trace。[/yellow]")
                return False
            trace = traces[0]
            stored = self._get_trace_store().get_trace(
                self.session_id,
                trace.trace_id,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must not exit the CLI
            self.print(f"[red]读取 Trace 失败：{type(exc).__name__}[/red]")
            return False
        if stored is None:
            self.print("[yellow]最近一次 Trace 已不存在。[/yellow]")
            return False
        trace, spans = stored
        shown_status = "incomplete" if trace.status == "running" else trace.status
        tokens = (
            f"{trace.input_tokens}/{trace.output_tokens}"
            if trace.input_tokens is not None and trace.output_tokens is not None
            else "n/a"
        )
        summary = Table.grid(padding=(0, 2))
        summary.add_row("Trace", trace.trace_id[:8], "Turn", trace.turn_id[:8])
        summary.add_row(
            "Status",
            f"{shown_status} / {trace.outcome or 'n/a'}",
            "Entry",
            trace.entrypoint,
        )
        summary.add_row(
            "Duration",
            self._trace_duration(trace.duration_ms),
            "Model",
            trace.model or "n/a",
        )
        summary.add_row(
            "Calls",
            (
                f"LLM {trace.llm_call_count} · Tool {trace.tool_call_count} · "
                f"Planning {trace.planning_call_count}"
            ),
            "Tokens",
            tokens,
        )
        if trace.error_type:
            summary.add_row("Error", trace.error_type, "", "")
        self.print(summary)
        self.print(self._trace_tree(trace, spans))
        return True

    def show_status(self) -> None:
        status = self._runtime_status()
        width = min(66, self._startup_width())
        available = max(1, width - 4)
        first_line = self._clip_startup(
            f"MODEL {status.model} · PROVIDER {status.provider}", available
        )
        second_line = self._clip_startup(
            f"SESSION {status.session[:12]} · {status.request_policy}", available
        )
        third_line = self._clip_startup(
            "TOOLS "
            f"{status.tool_count} · MEMORY {'on' if status.memory_enabled else 'off'} · "
            f"PLAN {'on' if status.planning_enabled else 'off'} · "
            f"TRACE {'on' if status.trace_enabled else 'off'}",
            available,
        )
        fourth_line = self._clip_startup(
            f"IMAGE {'configured' if status.image_enabled else 'off'} · "
            f"SEARCH {'configured' if status.search_enabled else 'off'}",
            available,
        )
        body = Text()
        body.append(f"{first_line}\n", style="#FFFFFF")
        body.append(f"{second_line}\n", style="#666666")
        body.append(f"{third_line}\n", style="#666666")
        body.append(fourth_line, style="#666666")
        if width < 40:
            self.print(body, overflow="ellipsis", no_wrap=True, crop=True)
            return
        self.print(
            Panel(
                body,
                title="当前运行状态",
                box=box.SQUARE,
                border_style="#666666",
                padding=(0, 1),
                width=width,
            )
        )

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
        if command_name == "/trace" and not command_argument:
            self.show_trace()
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
                    "[bold]/trace[/bold] 查看当前会话最近一次本地 Trace\n"
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
