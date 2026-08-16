import threading
from io import StringIO
from types import SimpleNamespace

import leon_agent.cli as cli_module
import pytest
from leon_agent.cli import LeonConsole, TerminalChatUI, _is_native_shift_enter, parse_args
from leon_agent.session import SessionStore
from prompt_toolkit.application import create_app_session
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from workbench_core.agent import AgentResult
from workbench_core.agent.runtime import cancellation_scope, current_cancel_event


class FailingAgent:
    def run(self, message, *, history):  # noqa: ANN001, ARG002
        raise RuntimeError("upstream failed")


class InterruptingAgent:
    def run(self, message, *, history):  # noqa: ANN001, ARG002
        raise KeyboardInterrupt


class SuccessfulAgent:
    def run(self, message, *, history):  # noqa: ANN001, ARG002
        return AgentResult(answer="ok")


def test_resume_command_maps_positional_session_id() -> None:
    args = parse_args(["resume", "session-123"])

    assert args.command == "resume"
    assert args.session == "session-123"


def test_legacy_session_option_remains_supported() -> None:
    args = parse_args(["--session", "session-123"])

    assert args.command is None
    assert args.session == "session-123"


@pytest.mark.parametrize(
    "argv",
    [
        ["resume"],
        ["resume", "session-123", "--session", "other-session"],
        ["resume", "session-123", "--new"],
    ],
)
def test_invalid_resume_arguments_exit_with_usage_error(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(argv)

    assert exc_info.value.code == 2


def test_failed_cli_turn_is_not_persisted(tmp_path) -> None:  # noqa: ANN001
    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.agent = FailingAgent()
    cli.console = Console(quiet=True)
    cli._progress = None
    cli._progress_task_id = None
    cli._ensure_current_provider = lambda: None  # type: ignore[method-assign]

    assert cli.process("这次会失败") is False
    assert cli.store.load_messages(cli.session_id) == []


def test_keyboard_interrupt_cancels_turn_without_traceback(tmp_path) -> None:  # noqa: ANN001
    output = StringIO()
    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.agent = InterruptingAgent()
    cli.console = Console(file=output, force_terminal=False)
    cli._progress = None
    cli._progress_task_id = None
    cli.llm_model = "test-model"
    cli._ensure_current_provider = lambda: None  # type: ignore[method-assign]

    assert cli.process("中断我") is False
    assert "本次请求已取消" in output.getvalue()
    assert cli.store.load_messages(cli.session_id) == []


def test_cli_prints_feedback_before_provider_call(tmp_path) -> None:  # noqa: ANN001
    output = StringIO()
    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.agent = SuccessfulAgent()
    cli.console = Console(file=output, force_terminal=False)
    cli._progress = None
    cli._progress_task_id = None
    cli.llm_model = "test-model"
    cli._ensure_current_provider = lambda: None  # type: ignore[method-assign]

    assert cli.process("你好") is True
    assert "正在请求模型" in output.getvalue()


def test_nsfw_command_bypasses_llm_and_defaults_to_marika(tmp_path) -> None:  # noqa: ANN001
    calls = []

    class FakeDirectTools:
        def execute(self, name, arguments):  # noqa: ANN001
            calls.append((name, arguments))
            return {
                "ok": True,
                "images": [{"image_url": "https://images.example/nsfw.png"}],
            }

    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.agent = FailingAgent()
    cli.direct_tools = FakeDirectTools()
    cli.image_client = type(
        "FakeImageClient",
        (),
        {
            "list_modes": lambda self: {
                "ok": True,
                "modes": [{"id": "k2_queen_marika"}, {"id": "k2_tifa_plus"}],
            }
        },
    )()
    cli.console = Console(quiet=True)
    cli._progress = None
    cli._progress_task_id = None
    cli._ensure_current_provider = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("NSFW command must not resolve or call the LLM")
    )

    assert cli.process("/NSFW 原样描述") is True
    assert calls == [
        (
            "generate_images",
            {
                "source_text": "原样描述",
                "workflow_ids": ["k2_queen_marika"],
                "batch_count": 1,
            },
        )
    ]
    assert cli.store.load_messages(cli.session_id) == [
        {"role": "user", "content": "/NSFW 原样描述"},
        {
            "role": "assistant",
            "content": "玛莉卡模式的图片生成好了。\n\n- https://images.example/nsfw.png",
        },
    ]


def test_switch_model_accepts_custom_model_id(tmp_path) -> None:  # noqa: ANN001
    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.console = Console(quiet=True)
    cli.model_selection = None
    cli.model_catalog = []
    cli.llm_scope = "toml:codex|https://new-api.abrdns.com/v1"

    class FakeSettings:
        profile = "toml:codex"
        active_base_url = "https://new-api.abrdns.com/v1"

    cli._resolve_llm_settings = lambda: FakeSettings()  # type: ignore[method-assign]

    def fake_create_agent():
        cli.llm_model = cli.model_selection[1]
        cli.llm_profile = cli.model_selection[0]
        return object()

    cli._create_agent = fake_create_agent  # type: ignore[method-assign]

    cli.switch_model("DeepSeek-V4-Pro")

    assert cli.store.get_model_selection(cli.session_id) == (
        "toml:codex|https://new-api.abrdns.com/v1",
        "DeepSeek-V4-Pro",
    )
    assert cli.llm_model == "DeepSeek-V4-Pro"


def test_switch_model_refreshes_catalog_after_provider_change(tmp_path) -> None:  # noqa: ANN001
    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.console = Console(quiet=True)
    cli.model_selection = None
    cli.model_catalog = ["old-provider-model"]
    cli.llm_scope = "old-provider|https://old.example/v1"
    calls = []

    def fake_ensure_current_provider() -> None:
        calls.append("ensure")
        cli.model_catalog = []
        cli.llm_scope = "new-provider|https://new.example/v1"

    def fake_fetch_model_catalog() -> list[str]:
        calls.append("fetch")
        cli.model_catalog = ["new-provider-model"]
        return cli.model_catalog

    cli._ensure_current_provider = fake_ensure_current_provider  # type: ignore[method-assign]
    cli._fetch_model_catalog = fake_fetch_model_catalog  # type: ignore[method-assign]

    class FakeSettings:
        profile = "new-provider"
        active_base_url = "https://new.example/v1"

    cli._resolve_llm_settings = lambda: FakeSettings()  # type: ignore[method-assign]

    def fake_create_agent():
        cli.llm_model = cli.model_selection[1]
        cli.llm_profile = cli.model_selection[0]
        return object()

    cli._create_agent = fake_create_agent  # type: ignore[method-assign]

    cli.switch_model("1")

    assert calls[:2] == ["ensure", "fetch"]
    assert cli.store.get_model_selection(cli.session_id) == (
        "new-provider|https://new.example/v1",
        "new-provider-model",
    )


def test_resume_session_switches_by_history_index_without_provider_call(tmp_path) -> None:  # noqa: ANN001
    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    first = cli.store.create_session()
    second = cli.store.create_session()
    cli.store.add_message(first, "user", "旧会话")
    cli.store.add_message(second, "user", "新会话")
    sessions = cli.store.list_sessions()
    target = sessions[0]["id"]
    current = sessions[1]["id"]
    cli.session_id = current
    cli.model_selection = None
    cli.console = Console(quiet=True)
    created_for = []

    def fake_create_agent():
        created_for.append(cli.session_id)
        return object()

    cli._create_agent = fake_create_agent  # type: ignore[method-assign]

    cli.resume_session("1")

    assert cli.session_id == target
    assert created_for == [target]
    expected_prompt = "新会话" if target == second else "旧会话"
    assert cli._last_user_message == expected_prompt
    assert cli.store.has_session(cli.session_id)


def test_history_list_marks_current_session_and_shows_last_user_preview(tmp_path) -> None:  # noqa: ANN001
    output = StringIO()
    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.store.add_message(cli.session_id, "user", "最后一条\n用户消息")
    cli.store.add_message(cli.session_id, "assistant", "回答")
    cli.console = Console(file=output, width=180, force_terminal=False)

    cli.show_history()

    rendered = output.getvalue()
    assert "Last user" in rendered
    assert "最后一条 用户消息" in rendered
    assert "*" in rendered


def test_resume_command_dispatches_id_to_session_switcher() -> None:
    calls = []
    cli = LeonConsole.__new__(LeonConsole)
    cli._check_active_turn = lambda: None  # type: ignore[method-assign]
    cli.resume_session = lambda value: calls.append(value)  # type: ignore[method-assign]

    assert cli.handle_interactive_message("/resume session-42") is True
    assert calls == ["session-42"]


def test_slash_commands_are_case_insensitive() -> None:
    calls = []
    cli = LeonConsole.__new__(LeonConsole)
    cli._check_active_turn = lambda: None  # type: ignore[method-assign]
    cli.resume_session = lambda value: calls.append(value)  # type: ignore[method-assign]

    assert cli.handle_interactive_message("/ReSuMe 2") is True
    assert calls == ["2"]


def test_tools_command_renders_registered_tool_schemas() -> None:
    output = StringIO()
    cli = LeonConsole.__new__(LeonConsole)
    cli.console = Console(file=output, width=160, force_terminal=False)
    cli.agent = SimpleNamespace(
        runtime=SimpleNamespace(
            tools=SimpleNamespace(
                schemas=[
                    {
                        "function": {
                            "name": "generate_images",
                            "description": "Generate images",
                        }
                    }
                ]
            )
        )
    )
    cli._check_active_turn = lambda: None  # type: ignore[method-assign]

    assert cli.handle_interactive_message("/TOOLS") is True

    rendered = output.getvalue()
    assert "generate_images" in rendered
    assert "Generate images" in rendered


def test_last_and_copy_use_latest_assistant_answer(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    output = StringIO()
    copied = []
    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.store.add_message(cli.session_id, "user", "问题")
    cli.store.add_message(cli.session_id, "assistant", "**最后回答**")
    cli.console = Console(file=output, width=120, force_terminal=False)
    cli._last_answer = ""
    cli._check_active_turn = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr(
        cli_module,
        "_copy_to_clipboard",
        lambda text: copied.append(text) or "fake-clipboard",
    )

    assert cli.handle_interactive_message("/LAST") is True
    assert cli.handle_interactive_message("/COPY") is True

    assert copied == ["**最后回答**"]
    assert "最后回答" in output.getvalue()
    assert "已复制上一条回答" in output.getvalue()


def test_copy_without_answer_is_a_noop(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.console = Console(quiet=True)
    cli._last_answer = ""
    monkeypatch.setattr(
        cli_module,
        "_copy_to_clipboard",
        lambda text: pytest.fail(f"clipboard called unexpectedly: {text}"),
    )

    assert cli.copy_last_answer() is False


def _make_process_cli(tmp_path, agent):  # noqa: ANN001
    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.agent = agent
    cli.console = Console(quiet=True)
    cli._progress = None
    cli._progress_task_id = None
    cli.llm_model = "test-model"
    cli._ensure_current_provider = lambda: None  # type: ignore[method-assign]
    return cli


def test_cancelled_late_cli_turn_is_not_persisted(tmp_path) -> None:  # noqa: ANN001
    started = threading.Event()
    release = threading.Event()

    class LateAgent:
        def run(self, message, *, history):  # noqa: ANN001, ARG002
            started.set()
            release.wait(timeout=2)
            return AgentResult(answer="late")

    cli = _make_process_cli(tmp_path, LateAgent())
    cancel_event = threading.Event()
    outcome = []

    def run() -> None:
        with cancellation_scope(cancel_event):
            outcome.append(cli.process("迟到结果"))

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(timeout=1)
    cancel_event.set()
    release.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert outcome == [False]
    assert cli.store.load_messages(cli.session_id) == []


def test_next_cli_turn_succeeds_after_cancelled_turn(tmp_path) -> None:  # noqa: ANN001
    started = threading.Event()
    release = threading.Event()

    class TwoTurnAgent:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, message, *, history):  # noqa: ANN001, ARG002
            self.calls += 1
            if self.calls == 1:
                started.set()
                release.wait(timeout=2)
                return AgentResult(answer="late")
            return AgentResult(answer="fresh")

    agent = TwoTurnAgent()
    cli = _make_process_cli(tmp_path, agent)
    cancel_event = threading.Event()
    outcome = []

    def run_first() -> None:
        with cancellation_scope(cancel_event):
            outcome.append(cli.process("第一轮"))

    worker = threading.Thread(target=run_first)
    worker.start()
    assert started.wait(timeout=1)
    cancel_event.set()
    release.set()
    worker.join(timeout=1)

    assert outcome == [False]
    assert cli.process("第二轮") is True
    assert cli.store.load_messages(cli.session_id) == [
        {"role": "user", "content": "第二轮"},
        {"role": "assistant", "content": "fresh"},
    ]


def test_retry_replays_last_prompt_and_appends_a_new_turn(tmp_path) -> None:  # noqa: ANN001
    class RetryAgent:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, message, *, history):  # noqa: ANN001, ARG002
            self.calls += 1
            return AgentResult(answer=f"answer-{self.calls}")

    agent = RetryAgent()
    cli = _make_process_cli(tmp_path, agent)

    assert cli.process("请再回答一次") is True
    assert cli.retry_last_message() is True

    assert agent.calls == 2
    assert cli.store.load_messages(cli.session_id) == [
        {"role": "user", "content": "请再回答一次"},
        {"role": "assistant", "content": "answer-1"},
        {"role": "user", "content": "请再回答一次"},
        {"role": "assistant", "content": "answer-2"},
    ]


def test_retry_reuses_cancelled_prompt_after_worker_returns(tmp_path) -> None:  # noqa: ANN001
    started = threading.Event()
    release = threading.Event()

    class RetryAfterCancelAgent:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, message, *, history):  # noqa: ANN001, ARG002
            self.calls += 1
            if self.calls == 1:
                started.set()
                release.wait(timeout=2)
                return AgentResult(answer="迟到")
            return AgentResult(answer="重试成功")

    agent = RetryAfterCancelAgent()
    cli = _make_process_cli(tmp_path, agent)
    cancel_event = threading.Event()
    outcome = []

    def run_first() -> None:
        with cancellation_scope(cancel_event):
            outcome.append(cli.process("网络抖了一下"))

    worker = threading.Thread(target=run_first)
    worker.start()
    assert started.wait(timeout=1)
    cancel_event.set()
    release.set()
    worker.join(timeout=1)

    assert outcome == [False]
    assert cli.retry_last_message() is True
    assert agent.calls == 2
    assert cli.store.load_messages(cli.session_id) == [
        {"role": "user", "content": "网络抖了一下"},
        {"role": "assistant", "content": "重试成功"},
    ]


def test_retry_without_any_prompt_is_a_noop(tmp_path) -> None:  # noqa: ANN001
    cli = _make_process_cli(tmp_path, SuccessfulAgent())

    assert cli.retry_last_message() is False
    assert cli.store.load_messages(cli.session_id) == []


def test_terminal_ui_ctrl_c_cancels_current_worker_and_ctrl_q_waits_for_exit(
    monkeypatch,
) -> None:  # noqa: ANN001
    started = threading.Event()
    release = threading.Event()

    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

        def handle_interactive_message(self, message):  # noqa: ANN001, ARG002
            started.set()
            active_event = current_cancel_event()
            assert active_event is not None
            release.wait(timeout=2)
            return True

        def _print_startup(self) -> None:
            return None

    class FakeBuffer:
        text = "hello"

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            self.exited = False

        def exit(self) -> None:
            self.exited = True

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    owner = FakeOwner()
    ui = TerminalChatUI(owner)
    owner.ui = ui
    fake_app = FakeApplication()
    ui.app = fake_app

    ui._accept(FakeBuffer())
    worker = ui._active_thread
    assert worker is not None
    assert worker.daemon is False
    assert started.wait(timeout=1)

    first_event = SimpleNamespace(app=fake_app)
    ui._handle_interrupt(first_event, exit_after=False)
    assert ui._active_cancel_event is not None
    assert ui._active_cancel_event.is_set()
    assert ui.busy is True
    assert fake_app.exited is False

    ui._handle_interrupt(first_event, exit_after=True)
    assert fake_app.exited is False
    release.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert ui.busy is False
    assert fake_app.exited is True


def test_terminal_ui_enter_sends_and_shift_enter_inserts_newline(monkeypatch) -> None:
    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            return None

        def invalidate(self) -> None:
            return None

    class FakeBuffer:
        def __init__(self) -> None:
            self.accepted = 0
            self.newlines = []

        def validate_and_handle(self) -> None:
            self.accepted += 1

        def newline(self, *, copy_margin) -> None:  # noqa: ANN001
            self.newlines.append(copy_margin)

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(FakeOwner())
    buffer = FakeBuffer()

    ui._handle_enter(
        SimpleNamespace(
            current_buffer=buffer,
            key_sequence=[SimpleNamespace(data="\r")],
        )
    )
    ui._handle_enter(
        SimpleNamespace(
            current_buffer=buffer,
            key_sequence=[SimpleNamespace(data="\x1b[27;2;13~")],
        )
    )
    ui._handle_enter(
        SimpleNamespace(
            current_buffer=buffer,
            key_sequence=[SimpleNamespace(data="\x1b[27;5;13~")],
        )
    )

    assert ui.input.buffer.multiline()
    assert ui._input_height().preferred == 1
    assert ui.input.window.preferred_height(80, 20).preferred == 1
    ui.input.buffer.text = "a\nb\nc\nd\ne\nf\ng"
    assert ui._input_height().preferred == 6
    assert ui.input.window.preferred_height(80, 20).preferred == 6
    assert buffer.accepted == 1
    assert buffer.newlines == [False, False]


def test_terminal_ui_command_completion_includes_descriptions(monkeypatch) -> None:
    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            return None

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(FakeOwner())

    completions = list(
        ui.input.completer.get_completions(
            Document("/re", cursor_position=3),
            CompleteEvent(completion_requested=True),
        )
    )

    assert [(item.text, item.display_meta_text) for item in completions] == [
        ("/resume", "切换已有会话"),
        ("/retry", "重试上一条请求"),
    ]


def test_terminal_ui_history_follows_session_and_preserves_draft(
    monkeypatch,
    tmp_path,
) -> None:  # noqa: ANN001
    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            return None

        def invalidate(self) -> None:
            return None

    store = SessionStore(tmp_path / "leon.db")
    first_session = store.create_session()
    second_session = store.create_session()
    store.add_message(first_session, "user", "第一问")
    store.add_message(first_session, "assistant", "第一答")
    store.add_message(first_session, "user", "第二问")
    store.add_message(second_session, "user", "另一个会话的问题")
    owner = SimpleNamespace(
        llm_model="fake-model",
        llm_provider_name="fake-provider",
        session_id=first_session,
        store=store,
    )
    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(owner)

    assert ui.input.buffer.history.get_strings() == ["第一问", "第二问"]

    ui.input.buffer.text = "未发送草稿"
    owner.session_id = second_session
    ui.refresh_input_history()

    assert ui.input.buffer.text == "未发送草稿"
    assert ui.input.buffer.history.get_strings() == ["另一个会话的问题"]


def test_terminal_ui_ctrl_d_deletes_draft_then_exits_when_empty(monkeypatch) -> None:
    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            self.exited = False

        def exit(self) -> None:
            self.exited = True

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(FakeOwner())
    ui.input.buffer.text = "draft"
    ui.input.buffer.cursor_position = 0
    event = SimpleNamespace(current_buffer=ui.input.buffer, app=ui.app)

    ui._handle_eof(event)

    assert ui.input.buffer.text == "raft"
    assert ui.app.exited is False

    ui.input.buffer.text = ""
    ui._handle_eof(event)

    assert ui.app.exited is True


def test_terminal_ui_input_editing_shortcuts(monkeypatch) -> None:
    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            return None

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(FakeOwner())
    buffer = ui.input.buffer

    buffer.text = "alpha beta   "
    buffer.cursor_position = len(buffer.text)
    ui._delete_previous_word(buffer)
    assert buffer.text == "alpha "

    buffer.text = "left right"
    buffer.cursor_position = len("left")
    ui._delete_to_end(buffer)
    assert buffer.text == "left"

    buffer.text = "clear everything"
    buffer.cursor_position = len("clear")
    ui._clear_input(buffer)
    assert buffer.text == ""

    buffer.text = "first line\nsecond line"
    buffer.cursor_position = len("first line\nsecond")
    ui._delete_to_end(buffer)
    assert buffer.text == "first line\nsecond"

    buffer.text = "first line\nsecond word"
    buffer.cursor_position = len("first line\nsecond word")
    ui._delete_previous_word(buffer)
    assert buffer.text == "first line\nsecond "

    buffer.text = "first line\nsecond line"
    buffer.cursor_position = len("first line\nsecond")
    ui._clear_input(buffer)
    assert buffer.text == "first line\n"


def test_copy_to_clipboard_uses_available_platform_helper(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(cli_module.sys, "platform", "linux")
    monkeypatch.setattr(
        cli_module.shutil,
        "which",
        lambda name: "/usr/bin/xclip" if name == "xclip" else None,
    )

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    executable = cli_module._copy_to_clipboard("answer")

    assert executable == "/usr/bin/xclip"
    assert calls[0][0] == ["/usr/bin/xclip", "-selection", "clipboard"]
    assert calls[0][1]["input"] == "answer"


def test_native_shift_enter_detection_rejects_ctrl_alt_and_other_keys() -> None:
    record = SimpleNamespace(VirtualKeyCode=13, ControlKeyState=0x0010)
    assert _is_native_shift_enter(record) is True
    assert _is_native_shift_enter(
        SimpleNamespace(VirtualKeyCode=13, ControlKeyState=0x0018)
    ) is False
    assert _is_native_shift_enter(
        SimpleNamespace(VirtualKeyCode=13, ControlKeyState=0x0011)
    ) is False
    assert _is_native_shift_enter(
        SimpleNamespace(VirtualKeyCode=65, ControlKeyState=0x0010)
    ) is False


@pytest.mark.parametrize(
    "newline_key",
    [
        pytest.param("\x1b[27;2;13~", id="xterm-shift-enter"),
        pytest.param("\x1b[13;2u", id="kitty-shift-enter"),
        pytest.param("\x1b[27;5;13~", id="xterm-ctrl-enter"),
        pytest.param("\x1b\r", id="escape-enter"),
    ],
)
def test_terminal_ui_modified_enter_sequences_create_multiline_prompt(
    newline_key,
) -> None:  # noqa: ANN001
    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

        def __init__(self) -> None:
            self.messages = []

        def handle_interactive_message(self, message):  # noqa: ANN001
            self.messages.append(message)
            return False

    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()):
            owner = FakeOwner()
            ui = TerminalChatUI(owner)
            owner.ui = ui
            ui.app.run(
                pre_run=lambda: pipe_input.send_text(f"one{newline_key}two\r")
            )

    assert owner.messages == ["one\ntwo"]
    assert ui.input.buffer.history.get_strings() == ["one\ntwo"]


def test_terminal_ui_keeps_draft_when_previous_turn_is_busy(monkeypatch) -> None:
    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            return None

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(FakeOwner())
    ui.busy = True
    ui.input.buffer.text = "第二轮草稿\n仍然保留"

    ui._handle_enter(
        SimpleNamespace(
            current_buffer=ui.input.buffer,
            key_sequence=[SimpleNamespace(data="\r")],
        )
    )

    assert ui.input.buffer.text == "第二轮草稿\n仍然保留"
    assert ui.input.buffer.history.get_strings() == []
    assert "草稿已保留" in ui.blocks[-1]
    assert ui._active_thread is None


def test_terminal_ui_ignores_whitespace_without_polluting_history(monkeypatch) -> None:
    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            return None

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(FakeOwner())
    ui.input.buffer.text = "   \n"

    ui.input.buffer.validate_and_handle()

    assert ui.input.buffer.text == ""
    assert ui.input.buffer.history.get_strings() == []
    assert ui._active_thread is None


def test_terminal_ui_preserves_multiline_prompt_indentation(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    messages = []

    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

        def handle_interactive_message(self, message):  # noqa: ANN001
            messages.append(message)
            started.set()
            release.wait(timeout=2)
            return True

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            self.exited = False

        def exit(self) -> None:
            self.exited = True

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(FakeOwner())
    ui.input.buffer.text = "  请解释代码\n    value = 1\n"

    ui.input.buffer.validate_and_handle()
    assert started.wait(timeout=1)
    worker = ui._active_thread
    assert worker is not None
    release.set()
    worker.join(timeout=1)

    assert messages == ["  请解释代码\n    value = 1"]
    assert ui.input.buffer.text == ""
    assert ui.input.buffer.history.get_strings() == ["  请解释代码\n    value = 1"]
    assert not worker.is_alive()


def test_interactive_chat_preserves_multiline_prompt_indentation() -> None:
    calls = []
    cli = LeonConsole.__new__(LeonConsole)
    cli._check_active_turn = lambda: None  # type: ignore[method-assign]
    cli.process = lambda message: calls.append(message)  # type: ignore[method-assign]

    assert cli.handle_interactive_message("  请解释代码\n    value = 1\n") is True
    assert calls == ["  请解释代码\n    value = 1"]


def test_interactive_nsfw_command_is_not_treated_as_unknown_slash_command() -> None:
    calls = []
    cli = LeonConsole.__new__(LeonConsole)
    cli.process = lambda message: calls.append(message)  # type: ignore[method-assign]

    assert cli.handle_interactive_message("/NSFW 原样描述") is True
    assert calls == ["/NSFW 原样描述"]
