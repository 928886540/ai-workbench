import threading
from io import StringIO
from types import SimpleNamespace

import pytest
from leon_agent.cli import LeonConsole, TerminalChatUI, parse_args
from leon_agent.session import SessionStore
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


def test_interactive_nsfw_command_is_not_treated_as_unknown_slash_command() -> None:
    calls = []
    cli = LeonConsole.__new__(LeonConsole)
    cli.process = lambda message: calls.append(message)  # type: ignore[method-assign]

    assert cli.handle_interactive_message("/NSFW 原样描述") is True
    assert calls == ["/NSFW 原样描述"]
