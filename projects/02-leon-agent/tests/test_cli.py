import sqlite3
import threading
from io import StringIO
from types import SimpleNamespace

import leon_agent.cli as cli_module
import pytest
from leon_agent.cli import (
    LeonConsole,
    TerminalChatUI,
    _is_native_shift_enter,
    _legacy_prompt_markup,
    parse_args,
)
from leon_agent.file_write_policy import file_write_turn
from leon_agent.session import SessionStore
from prompt_toolkit.application import create_app_session
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from workbench_core.agent import AgentResult, ToolStep
from workbench_core.agent.runtime import (
    AgentCancelled,
    cancellation_scope,
    current_cancel_event,
)


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


def test_cli_normalises_win32_surrogate_pair_before_request_and_storage(tmp_path) -> None:
    calls = []

    class EmojiAgent:
        def run(self, message, *, history):  # noqa: ANN001, ARG002
            calls.append(message)
            return AgentResult(answer="收到：\ud83d\ude02")

    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.agent = EmojiAgent()
    cli.console = Console(quiet=True)
    cli.ui = None
    cli._progress = None
    cli._progress_task_id = None
    cli._ensure_current_provider = lambda: None  # type: ignore[method-assign]

    assert cli.process("哈哈\ud83d\ude02") is True
    assert calls == ["哈哈😂"]
    assert cli.store.load_messages(cli.session_id) == [
        {"role": "user", "content": "哈哈😂"},
        {"role": "assistant", "content": "收到：😂"},
    ]


def test_generic_tui_failure_does_not_report_image_generation_failure(tmp_path) -> None:
    class FakeUI:
        def __init__(self) -> None:
            self.lines = []

        def _set_status(self, text, *, animate=False):  # noqa: ANN001, ARG002
            return None

        def write_plain(self, text):  # noqa: ANN001
            self.lines.append(text)

        def write_rich(self, *objects, **kwargs):  # noqa: ANN001, ANN003
            self.lines.extend(str(item) for item in objects)

    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.agent = FailingAgent()
    cli.console = Console(quiet=True)
    cli.ui = FakeUI()
    cli._progress = None
    cli._progress_task_id = None
    cli._image_progress_active = False
    cli._ensure_current_provider = lambda: None  # type: ignore[method-assign]

    assert cli.process("普通聊天失败") is False
    assert not any("图片生成失败" in line for line in cli.ui.lines)
    assert any("请求失败" in line for line in cli.ui.lines)


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
    assert "正在思考中" in output.getvalue()
    assert "正在请求模型" not in output.getvalue()


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


def test_cli_background_image_completion_persists_and_renders(tmp_path) -> None:  # noqa: ANN001
    class FakeUI:
        def __init__(self) -> None:
            self.removed = []
            self.answers = []
            self.lines = []

        def remove_background_image_jobs(self, job_ids):  # noqa: ANN001
            self.removed.extend(job_ids)

        def write_answer(self, answer):  # noqa: ANN001
            self.answers.append(answer)

        def write_plain(self, text):  # noqa: ANN001
            self.lines.append(text)

    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.ui = FakeUI()
    cli._last_answer = ""
    cli._last_image_url = None

    cli._finish_image_submission(
        cli.session_id,
        ["job-1"],
        {
            "ok": True,
            "images": [
                {"job_id": "job-1", "image_url": "https://example.test/result.png"}
            ],
        },
    )

    assert cli.ui.removed == ["job-1"]
    assert cli.ui.answers == [
        "1 张图片生成好了。\n\n- https://example.test/result.png"
    ]
    assert cli.store.load_messages(cli.session_id) == [
        {
            "role": "assistant",
            "content": "1 张图片生成好了。\n\n- https://example.test/result.png",
        }
    ]
    assert cli._last_image_url == "https://example.test/result.png"


def test_cli_background_tracker_delivers_completed_image(tmp_path) -> None:  # noqa: ANN001
    delivered = threading.Event()

    class FakeImageClient:
        def get_image_tasks(self, *, chat_id, limit):  # noqa: ANN001
            assert chat_id.startswith("leon-agent:")
            assert limit >= 20
            return {
                "ok": True,
                "items": [
                    {
                        "job_id": "job-fast",
                        "status": "completed",
                        "image_url": "https://example.test/fast.png",
                    }
                ],
            }

        def get_recent_images(self, *, chat_id, limit):  # noqa: ANN001, ARG002
            raise AssertionError("completed task URL should not query gallery")

    class FakeUI:
        def __init__(self) -> None:
            self.active = set()
            self.answers = []

        def add_background_image_jobs(self, job_ids):  # noqa: ANN001
            self.active.update(job_ids)

        def remove_background_image_jobs(self, job_ids):  # noqa: ANN001
            self.active.difference_update(job_ids)

        def write_answer(self, answer):  # noqa: ANN001
            self.answers.append(answer)
            delivered.set()

        def write_plain(self, text):  # noqa: ANN001, ARG002
            return None

    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.image_client = FakeImageClient()
    cli.ui = FakeUI()
    cli._last_answer = ""
    cli._last_image_url = None
    cli._background_image_lock = threading.RLock()
    cli._tracked_image_jobs = set()
    cli._background_image_threads = set()

    cli._on_generation_submitted(
        {"ok": True, "jobs": [{"job_id": "job-fast", "status": "queued"}]}
    )

    assert delivered.wait(timeout=2)
    assert cli.ui.active == set()
    assert cli.ui.answers == [
        "1 张图片生成好了。\n\n- https://example.test/fast.png"
    ]
    assert cli.store.load_messages(cli.session_id)[-1]["content"].endswith(
        "https://example.test/fast.png"
    )


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


def test_restore_last_exchange_recovers_latest_image_for_resume(tmp_path) -> None:  # noqa: ANN001
    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.store.add_message(cli.session_id, "user", "生成第一张")
    cli.store.add_message(cli.session_id, "assistant", "第一张 https://example.test/one.png")
    cli.store.add_message(cli.session_id, "user", "生成第二张")
    cli.store.add_message(cli.session_id, "assistant", "第二张 https://example.test/two.png")

    cli._restore_last_exchange()

    assert cli._last_user_message == "生成第二张"
    assert cli._last_answer == "第二张 https://example.test/two.png"
    assert cli._last_image_url == "https://example.test/two.png"


def test_print_resume_context_replays_session_history_and_keeps_latest_turn(
    tmp_path,
) -> None:  # noqa: ANN001
    calls = []

    class FakeUI:
        def write_user_message(self, message):  # noqa: ANN001
            calls.append(("user", message))

        def write_answer(self, answer):  # noqa: ANN001
            calls.append(("assistant", answer))

        def write_turn_separator(self):
            calls.append(("separator", None))

    cli = LeonConsole.__new__(LeonConsole)
    cli.ui = FakeUI()
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.store.add_message(cli.session_id, "user", "我操")
    cli.store.add_message(cli.session_id, "assistant", "第一轮回答")
    cli.store.add_message(cli.session_id, "user", "生成一张图")
    cli.store.add_message(
        cli.session_id,
        "assistant",
        "生成好了 https://example.test/result.png",
    )
    cli._restore_last_exchange()

    cli._print_resume_context()

    assert calls == [
        ("user", "我操"),
        ("assistant", "第一轮回答"),
        ("separator", None),
        ("user", "生成一张图"),
        ("assistant", "生成好了 https://example.test/result.png"),
        ("separator", None),
    ]
    assert cli._last_user_message == "生成一张图"
    assert cli._last_answer == "生成好了 https://example.test/result.png"
    assert cli._last_image_url == "https://example.test/result.png"


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


def test_terminal_ui_ctrl_insert_copies_without_adding_a_chat_block(monkeypatch) -> None:
    calls = []

    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

        def copy_last_answer(self, *, quiet):  # noqa: ANN001
            calls.append(quiet)
            return True

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.key_bindings = kwargs["key_bindings"]

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(FakeOwner())
    binding = next(
        item
        for item in ui.app.key_bindings.bindings
        if item.keys == (Keys.ControlInsert,)
    )

    binding.handler(SimpleNamespace())

    assert calls == [True]
    assert ui.status_text == "● 已复制上一条回答"
    assert ui.blocks == []


def test_status_reports_unlimited_llm_response_wait() -> None:
    output = StringIO()
    cli = LeonConsole.__new__(LeonConsole)
    cli.console = Console(file=output, width=120, force_terminal=False)
    cli.llm_model = "test-model"
    cli.llm_provider_name = "test-provider"
    cli.session_id = "test-session"
    cli.llm_timeout_seconds = 0.0
    cli.llm_max_retries = 0
    cli.config = SimpleNamespace(backend_url="http://backend.example")
    cli.search_service = None

    cli.show_status()

    rendered = output.getvalue()
    assert "response=unlimited" in rendered
    assert "timeout=0s" not in rendered


def test_timeout_error_does_not_describe_unlimited_reads_as_zero_seconds() -> None:
    cli = LeonConsole.__new__(LeonConsole)
    cli.llm_timeout_seconds = 0.0
    cli.llm_max_retries = 0
    error = type("APITimeoutError", (Exception,), {})()

    rendered = cli._format_request_error(error)

    assert "响应等待不限时" in rendered
    assert "0s" not in rendered


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


def _make_composition_cli(monkeypatch, tmp_path, file_roots):  # noqa: ANN001
    captured = {}

    class FakeSettings:
        profile = "toml:test"
        active_base_url = "http://llm.example/v1"
        llm_source = "toml"
        codex_config_path = "test-config.toml"
        llm_timeout_seconds = 0.0
        llm_max_retries = 0

    class FakeLLMClient:
        model = "test-model"
        profile = "toml:test"

        def __init__(self, settings, model_override=None):  # noqa: ANN001, ARG002
            return None

    class FakeImageClient:
        def __init__(self, **kwargs):  # noqa: ANN003, ARG002
            return None

    class FakeLeonAgent:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured["agent_kwargs"] = kwargs

    cli = LeonConsole.__new__(LeonConsole)
    cli.session_id = "composition-session"
    cli.model_selection = None
    cli.memory_store = cli_module.MemoryStore(tmp_path / "leon.db")
    cli.config = SimpleNamespace(
        backend_url="http://backend.example",
        active_plugin_dir=tmp_path,
        active_public_image_base_url="http://images.example",
        http_timeout_seconds=1.0,
        bridge_timeout_seconds=1.0,
        tavily_api_key=None,
        tavily_base_url="https://api.tavily.com",
        tavily_fallback_api_key=None,
        tavily_fallback_base_url=None,
        tavily_timeout_seconds=1.0,
        tavily_max_results=5,
        file_roots=file_roots,
        session_db=tmp_path / "leon.db",
        default_mode_ids=["k2_tifa_plus"],
        read_additional_system_prompt=lambda: None,
    )
    cli._resolve_llm_settings = lambda: FakeSettings()  # type: ignore[method-assign]
    monkeypatch.setattr(cli_module, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(cli_module, "LeonImageClient", FakeImageClient)
    monkeypatch.setattr(cli_module, "LeonAgent", FakeLeonAgent)

    cli._create_agent()
    return cli, captured


def test_cli_file_write_composition_is_disabled_without_roots(monkeypatch, tmp_path) -> None:
    cli, captured = _make_composition_cli(monkeypatch, tmp_path, {})

    assert cli.file_service is None
    assert cli.file_write_service is None
    assert {"create_file", "write_file"}.isdisjoint(cli.direct_tools.names)
    assert captured["agent_kwargs"]["file_service"] is None
    assert captured["agent_kwargs"]["file_write_service"] is None
    assert captured["agent_kwargs"]["wait_for_image_completion"] is False
    assert captured["agent_kwargs"]["on_generation_submitted"].__self__ is cli
    generation_service = cli.direct_tools._tools["generate_images"].handler.__self__
    assert generation_service.wait_for_image_completion is False
    assert generation_service.on_generation_submitted.__self__ is cli


def test_cli_file_write_composition_reuses_matching_service(monkeypatch, tmp_path) -> None:
    cli, captured = _make_composition_cli(monkeypatch, tmp_path, {"docs": tmp_path})

    assert cli.file_service is not None
    assert cli.file_write_service is not None
    assert {"create_file", "write_file"}.issubset(cli.direct_tools.names)
    assert cli.file_service.root_bindings == cli.file_write_service.root_bindings
    assert captured["agent_kwargs"]["file_service"] is cli.file_service
    assert captured["agent_kwargs"]["file_write_service"] is cli.file_write_service


def test_cli_once_mode_keeps_synchronous_image_result(monkeypatch, tmp_path) -> None:
    cli, captured = _make_composition_cli(monkeypatch, tmp_path, {})
    cli.background_image_tracking = False

    cli._create_agent()

    assert captured["agent_kwargs"]["wait_for_image_completion"] is True
    assert captured["agent_kwargs"]["on_generation_submitted"] is None
    generation_service = cli.direct_tools._tools["generate_images"].handler.__self__
    assert generation_service.wait_for_image_completion is True
    assert generation_service.on_generation_submitted is None


def test_cli_memory_composition_uses_shared_db_and_current_session(monkeypatch, tmp_path) -> None:
    cli, captured = _make_composition_cli(monkeypatch, tmp_path, {})

    first_service = cli.memory_service
    assert first_service is captured["agent_kwargs"]["memory_service"]
    assert first_service.store.path == (tmp_path / "leon.db").resolve()
    assert first_service.session_id == "composition-session"
    assert {"memory_get", "memory_upsert", "memory_delete"}.isdisjoint(
        cli.direct_tools.names
    )

    cli.session_id = "resumed-session"
    cli._create_agent()

    assert cli.memory_service is not first_service
    assert cli.memory_service.store is cli.memory_store
    assert cli.memory_service.session_id == "resumed-session"


def test_cli_file_write_budget_resets_between_turns(monkeypatch, tmp_path) -> None:
    cli, _ = _make_composition_cli(monkeypatch, tmp_path, {"docs": tmp_path})
    service = cli.file_write_service
    assert service is not None

    with file_write_turn(service, "!file create docs:first.md"):
        first = cli.direct_tools.execute(
            "create_file",
            {"root_id": "docs", "relative_path": "first.md", "content": "first"},
        )
    with file_write_turn(service, "!file create docs:second.md"):
        second = cli.direct_tools.execute(
            "create_file",
            {"root_id": "docs", "relative_path": "second.md", "content": "second"},
        )

    assert first["ok"] is True
    assert second["ok"] is True
    assert (tmp_path / "first.md").read_text(encoding="utf-8") == "first"
    assert (tmp_path / "second.md").read_text(encoding="utf-8") == "second"


def test_cancelled_cli_turn_persists_only_safe_partial_tool_audit(tmp_path) -> None:
    answer_marker = "cancelled-answer-must-not-persist"
    content_marker = "raw-file-content-must-not-persist"

    class PartiallyCompletedAgent:
        def run(self, message, *, history):  # noqa: ANN001, ARG002
            partial = AgentResult(
                answer=answer_marker,
                messages=[{"role": "assistant", "content": content_marker}],
                steps=[
                    ToolStep(
                        "create_file",
                        {"root_id": "docs", "relative_path": "note.md"},
                        {
                            "ok": True,
                            "created": True,
                            "root_id": "docs",
                            "path": "note.md",
                            "citation": "docs:note.md",
                            "bytes": 4,
                        },
                    )
                ],
            )
            raise AgentCancelled(partial_result=partial)

    cli = _make_process_cli(tmp_path, PartiallyCompletedAgent())

    assert cli.process("!file create docs:note.md\n" + content_marker) is False
    assert cli.store.load_messages(cli.session_id) == []

    with sqlite3.connect(cli.store.path) as connection:
        rows = connection.execute(
            "SELECT name, arguments_json, result_json FROM tool_calls WHERE session_id = ?",
            (cli.session_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "create_file"
    assert content_marker not in repr(rows[0])
    assert answer_marker not in repr(rows[0])


def test_cli_cancel_after_agent_result_preserves_completed_tool_audit(tmp_path) -> None:
    agent_returned = False

    class CompletedToolAgent:
        def run(self, message, *, history):  # noqa: ANN001, ARG002
            nonlocal agent_returned
            agent_returned = True
            return AgentResult(
                answer="late answer",
                steps=[
                    ToolStep(
                        "create_file",
                        {"root_id": "docs", "relative_path": "note.md"},
                        {
                            "ok": True,
                            "created": True,
                            "root_id": "docs",
                            "path": "note.md",
                            "citation": "docs:note.md",
                            "bytes": 4,
                        },
                    )
                ],
            )

    cli = _make_process_cli(tmp_path, CompletedToolAgent())

    def cancel_after_result() -> None:
        if agent_returned:
            raise AgentCancelled()

    cli._check_active_turn = cancel_after_result  # type: ignore[method-assign]

    assert cli.process("create a file") is False
    assert cli.store.load_messages(cli.session_id) == []
    with sqlite3.connect(cli.store.path) as connection:
        rows = connection.execute(
            "SELECT name FROM tool_calls WHERE session_id = ?",
            (cli.session_id,),
        ).fetchall()
    assert rows == [("create_file",)]


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
    assert worker.daemon is True
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


def test_terminal_ui_ctrl_c_clears_then_exits_when_idle(monkeypatch) -> None:
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
    event = SimpleNamespace(app=ui.app)

    ui.input.buffer.text = "未发送草稿"
    ui._handle_ctrl_c(event)
    assert ui.app.exited is False
    assert ui.input.buffer.text == ""

    ui._handle_ctrl_c(event)
    assert ui.app.exited is True


def test_terminal_ui_ctrl_c_second_press_interrupts_busy_turn_and_third_exits(
    monkeypatch,
) -> None:  # noqa: ANN001
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
    ui.busy = True
    ui._active_cancel_event = threading.Event()
    calls = []

    def record_interrupt(event, **kwargs):  # noqa: ANN001, ANN003
        calls.append(kwargs)

    ui._handle_interrupt = record_interrupt  # type: ignore[method-assign]
    event = SimpleNamespace(app=ui.app)

    ui.input.buffer.text = "未发送草稿"
    ui._handle_ctrl_c(event)
    assert ui.input.buffer.text == ""
    assert calls == []

    ui._handle_ctrl_c(event)
    assert calls == [
        {
            "exit_after": False,
            "message_override": "⏹ 本轮已取消；再次按 Ctrl+C 退出。",
            "status_override": "⏹ 已取消 · 再按 Ctrl+C 退出",
        }
    ]
    assert ui.app.exited is False

    ui._handle_ctrl_c(event)
    assert calls[-1] == {"exit_after": True}
    assert ui.app.exited is False


def test_terminal_ui_ctrl_c_sequence_resets_after_a_normal_clear_edit(monkeypatch) -> None:
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
    event = SimpleNamespace(app=ui.app)

    ui.input.buffer.text = "第一份草稿"
    ui._handle_ctrl_c(event)
    assert ui._ctrl_c_count == 1

    # Ctrl+U/other editing clears the composer and must break the sequence.
    ui.input.buffer.text = "第二份草稿"
    ui._clear_input(ui.input.buffer)
    assert ui.input.buffer.text == ""
    assert ui._ctrl_c_count == 0
    assert ui.status_text == TerminalChatUI._IDLE_STATUS
    assert ui.status.height().preferred == 0

    ui._handle_ctrl_c(event)
    assert ui._ctrl_c_count == 1
    assert ui.app.exited is True


def test_terminal_ui_second_ctrl_c_does_not_exit_if_turn_finishes_during_cancel(
    monkeypatch,
) -> None:  # noqa: ANN001
    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            self.exit_calls = 0

        def exit(self) -> None:
            self.exit_calls += 1

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(FakeOwner())
    ui.busy = True
    ui._active_cancel_event = threading.Event()
    event = SimpleNamespace(app=ui.app)

    ui._handle_ctrl_c(event)
    original_interrupt = ui._handle_interrupt

    def finish_turn_before_interrupt(event, **kwargs):  # noqa: ANN001, ANN003
        with ui.lock:
            ui.busy = False
            ui._active_cancel_event = None
        original_interrupt(event, **kwargs)

    ui._handle_interrupt = finish_turn_before_interrupt  # type: ignore[method-assign]
    ui._handle_ctrl_c(event)

    assert ui._ctrl_c_count == 2
    assert ui._exit_requested is False
    assert ui.app.exit_calls == 0


def test_terminal_ui_repeated_escape_cancels_without_requesting_exit(monkeypatch) -> None:
    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            self.exit_calls = 0

        def exit(self) -> None:
            self.exit_calls += 1

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(FakeOwner())
    ui.busy = True
    ui._active_cancel_event = threading.Event()
    event = SimpleNamespace(app=ui.app)

    ui._handle_interrupt(event, exit_after=False)
    ui._handle_interrupt(event, exit_after=False)

    assert ui._active_cancel_event.is_set()
    assert ui._exit_requested is False
    assert ui.app.exit_calls == 0


def test_print_resume_hint_includes_current_session(tmp_path) -> None:  # noqa: ANN001
    output = StringIO()
    cli = LeonConsole.__new__(LeonConsole)
    cli.console = Console(file=output, force_terminal=False)
    cli.session_id = "session-to-resume"

    cli.print_resume_hint()

    assert "leon resume session-to-resume" in output.getvalue()


def test_terminal_ui_model_picker_accepts_number_without_sending_chat(monkeypatch) -> None:
    selected = []

    class FakeOwner:
        llm_model = "grok-4.6"
        llm_provider_name = "custom"
        session_id = "session-test"

        def switch_model(self, value) -> None:  # noqa: ANN001
            selected.append(value)

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            return None

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(FakeOwner())
    ui.begin_model_picker(["grok-4.6", "gpt-5.6-sol"], current="grok-4.6")
    ui.input.buffer.text = "2"

    ui._accept(ui.input.buffer)

    assert selected == ["2"]
    assert ui._model_picker is None
    assert ui.input.buffer.text == ""
    assert all(block != "» 2" for block in ui.blocks)


def test_terminal_ui_hides_idle_status_and_only_shows_composer_hint_for_input(
    monkeypatch,
) -> None:
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

    assert ui._status_fragments() == []
    assert ui._status_line() == ""
    assert ui.status.height().preferred == 0
    assert ui._composer_hint_fragments() == []

    ui.input.buffer.text = "/"
    hint_text = "".join(fragment[1] for fragment in ui._bottom_bar_fragments())
    assert "Tab 补全命令" in hint_text
    assert "fake-model" not in hint_text
    ui.input.buffer.text = "普通问题"
    ui.busy = True
    busy_text = "".join(fragment[1] for fragment in ui._bottom_bar_fragments())
    assert "Enter 加入队列" in busy_text
    ui.input.buffer.text = ""
    idle_text = "".join(fragment[1] for fragment in ui._bottom_bar_fragments())
    assert "fake-model" in idle_text

    ui.busy = False
    ui.add_background_image_jobs(["job-1"])
    background_status = "".join(fragment[1] for fragment in ui._status_fragments())
    assert "后台生图 1 项" in background_status
    assert ui.status.height().preferred == 1
    ui.remove_background_image_jobs(["job-1"])
    assert ui.status.height().preferred == 0
    assert "Enter 发送" not in idle_text


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
    assert ui.input.window.dont_extend_height() is True
    assert ui.input.window.height.min == 1
    assert ui.input.window.height.max == 6
    ui.input.buffer.text = "a\nb\nc\nd\ne\nf\ng"
    assert ui.input.buffer.document.line_count == 7
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


def test_terminal_ui_uses_inline_scrollback_native_selection_and_blinking_cursor(
    monkeypatch,
) -> None:
    application_kwargs = {}

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            application_kwargs.update(kwargs)

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(
        SimpleNamespace(
            llm_model="fake-model",
            llm_provider_name="fake-provider",
            session_id="session-test",
        )
    )

    assert ui.app is not None
    assert application_kwargs["full_screen"] is False
    assert application_kwargs["mouse_support"] is False
    assert application_kwargs["cursor"] == cli_module.CursorShape.BLINKING_BEAM
    assert application_kwargs["refresh_interval"] is None
    prompt_processor = ui.input.control.input_processors[2]
    assert prompt_processor.text == [("class:composer.prompt", "» ")]
    assert ui.input.window.always_hide_cursor() is False
    ui._cursor_visible = False
    assert ui.input.window.always_hide_cursor() is True


def test_terminal_ui_page_scroll_changes_output_position(monkeypatch) -> None:
    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            self.invalidations = 0

        def invalidate(self) -> None:
            self.invalidations += 1

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(
        SimpleNamespace(
            llm_model="fake-model",
            llm_provider_name="fake-provider",
            session_id="session-test",
        )
    )
    ui.blocks = [f"message {index}" for index in range(30)]
    ui.output.render_info = SimpleNamespace(window_height=6)
    ui.output.vertical_scroll = 20

    ui._scroll_output_page(-1)
    assert ui.output.vertical_scroll == 15
    assert ui._follow_output is False

    ui.output.vertical_scroll = 58
    ui._scroll_output_page(1)
    assert ui.output.vertical_scroll == 58
    assert ui._follow_output is True
    assert ui.app.invalidations == 2


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


def test_terminal_ui_multiline_composer_has_no_artificial_indent() -> None:
    assert TerminalChatUI._composer_line_prefix(0, 0) == ""
    assert TerminalChatUI._composer_line_prefix(1, 0) == ""
    assert TerminalChatUI._composer_line_prefix(0, 1) == ""


def test_terminal_ui_history_has_an_empty_cursor_after_last_entry(monkeypatch) -> None:
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
    ui._history_entries = ["第一条", "第二条"]
    ui._history_index = len(ui._history_entries)
    ui.input.buffer.text = ""

    ui._history_or_cursor(ui.input.buffer, direction=-1)
    assert ui.input.buffer.text == "第二条"
    ui._history_or_cursor(ui.input.buffer, direction=-1)
    assert ui.input.buffer.text == "第一条"
    ui._history_or_cursor(ui.input.buffer, direction=1)
    assert ui.input.buffer.text == "第二条"
    ui._history_or_cursor(ui.input.buffer, direction=1)
    assert ui.input.buffer.text == ""
    ui._history_or_cursor(ui.input.buffer, direction=1)
    assert ui.input.buffer.text == ""


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


def test_copy_to_clipboard_uses_utf16_for_windows_clip(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    monkeypatch.setattr(
        cli_module.shutil,
        "which",
        lambda name: "C:/Windows/clip.exe" if name == "clip.exe" else None,
    )

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module._copy_to_clipboard("emoji 😀 中文") == "C:/Windows/clip.exe"
    assert calls[0][1]["text"] is False
    assert calls[0][1]["input"] == "emoji 😀 中文".encode("utf-16le")


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


def test_terminal_ui_queues_message_when_previous_turn_is_busy(monkeypatch) -> None:
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

    assert ui.input.buffer.text == ""
    assert ui.input.buffer.history.get_strings() == []
    assert list(ui._queued_messages) == ["第二轮草稿\n仍然保留"]
    assert "已加入消息队列" in ui.blocks[-1]
    assert ui._active_thread is None


def test_terminal_ui_runs_queued_message_after_current_turn(monkeypatch) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    calls = []

    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

        def handle_interactive_message(self, message):  # noqa: ANN001
            calls.append(message)
            if message == "第一轮":
                first_started.set()
                release_first.wait(timeout=2)
            else:
                second_finished.set()
            return True

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003, ARG002
            self.exited = False

        def invalidate(self) -> None:
            return None

        def exit(self) -> None:
            self.exited = True

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    owner = FakeOwner()
    ui = TerminalChatUI(owner)
    owner.ui = ui

    ui.input.buffer.text = "第一轮"
    ui._accept(ui.input.buffer)
    first_worker = ui._active_thread
    assert first_worker is not None
    assert first_started.wait(timeout=1)

    ui.input.buffer.text = "第二轮"
    ui._accept(ui.input.buffer)
    assert list(ui._queued_messages) == ["第二轮"]

    release_first.set()
    first_worker.join(timeout=1)
    assert second_finished.wait(timeout=1)
    second_worker = ui._active_thread
    if second_worker is not None:
        second_worker.join(timeout=1)

    assert calls == ["第一轮", "第二轮"]
    assert list(ui._queued_messages) == []
    assert ui.busy is False
    assert sum("Worked for" in block for block in ui.blocks) == 1
    assert any(block and set(block) == {"─"} for block in ui.blocks)


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


def test_terminal_ui_uses_two_unframed_composer_lines_and_delays_user_render(
    monkeypatch,
) -> None:  # noqa: ANN001
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

    assert ui.composer_top.char == "─"
    assert ui.composer_bottom.char == "─"
    assert ui.output_gap.height == 1
    assert ui.output.right_margins == []
    ui.input.buffer.text = "尚未发送的草稿"
    assert all("尚未发送的草稿" not in block for block in ui.blocks)

    ui.write_user_message("已经发送")

    assert ui.blocks[-1] == "» 已经发送"
    assert "你" not in ui.blocks[-1]


def test_terminal_ui_answer_uses_bullet_without_leon_and_indents_following_lines(
    monkeypatch,
) -> None:  # noqa: ANN001
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

    ui.write_answer("第一行\n\n第二行")

    lines = ui.blocks[-1].splitlines()
    assert lines[0].rstrip() == "• 第一行"
    assert lines[-1] == "  第二行"
    assert all("Leon" not in line for line in lines)
    assert all(line.startswith("  ") for line in lines[1:])


def test_terminal_ui_turn_separator_retires_old_timing_and_colors_markers(
    monkeypatch,
) -> None:
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
    monkeypatch.setattr(ui, "_render_width", lambda: 48)

    ui.write_answer("旧回答")
    ui.write_turn_separator(174.9)
    assert ui.blocks[-1].startswith("─ Worked for 2m 54s ")

    ui._retire_worked_separator()
    assert ui.blocks[-1] == "─" * 48
    assert all("Worked for" not in block for block in ui.blocks)

    ui.write_answer("新回答")
    ui.write_turn_separator(5.9)
    assert sum("Worked for" in block for block in ui.blocks) == 1
    ui.write_plain("✓ tool 完成")
    ui.write_plain("✗ tool 失败")
    fragments = ui._output_fragments()

    marker_styles = [style for style, text, *_ in fragments if text == "• "]
    assert marker_styles == ["class:message.marker.old", "class:message.marker"]
    assert any(
        style == "class:status.success" and text == "✓ tool 完成"
        for style, text, *_ in fragments
    )
    assert any(
        style == "class:status.error" and text == "✗ tool 失败"
        for style, text, *_ in fragments
    )


def test_terminal_ui_answer_list_does_not_leave_a_lonely_outer_bullet(monkeypatch) -> None:
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
    ui.write_answer("- 第一项\n- 第二项")

    lines = ui.blocks[-1].splitlines()
    assert lines[0] == "• 第一项"
    assert lines[1].lstrip() == "• 第二项"
    assert all(not line.endswith(" ") for line in lines)


def test_terminal_ui_continuation_lines_keep_assistant_palette(monkeypatch) -> None:
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
    ui.write_plain("• 第一行\n  第二行")

    assert any(
        fragment[0] == "class:message.assistant" and "第二行" in fragment[1]
        for fragment in ui._output_fragments()
    )


def test_startup_uses_compact_unframed_summary_on_narrow_terminal() -> None:
    output = StringIO()
    cli = LeonConsole.__new__(LeonConsole)
    cli.console = Console(file=output, width=18, force_terminal=False)
    cli.ui = None
    cli.llm_model = "very-long-model-name"
    cli.llm_provider_name = "very-long-provider-name"
    cli.llm_profile = "fallback-profile"
    cli.session_id = "session-1234567890"

    cli._print_startup()

    rendered = output.getvalue()
    assert "╭" not in rendered
    assert "✦ LEON" in rendered
    assert "/help /model" not in rendered
    assert "Commands:" not in rendered
    assert len(rendered.splitlines()) <= 5


def test_startup_hides_command_catalog_in_normal_width() -> None:
    output = StringIO()
    cli = LeonConsole.__new__(LeonConsole)
    cli.console = Console(file=output, width=80, force_terminal=False)
    cli.ui = None
    cli.llm_model = "gpt-5.6-sol"
    cli.llm_provider_name = "custom"
    cli.llm_profile = "custom"
    cli.session_id = "session-1234567890"

    cli._print_startup()

    rendered = output.getvalue()
    assert "Tip:" not in rendered
    assert "Commands:" not in rendered
    assert "/model 选择" not in rendered
    assert "Model" in rendered
    assert "Provider" in rendered
    assert "Endpoint" in rendered
    assert "Session" in rendered
    assert "/model" in rendered
    assert "/tools" in rendered


def test_legacy_answer_and_feedback_match_compact_tui_contract() -> None:
    output = StringIO()
    cli = LeonConsole.__new__(LeonConsole)
    cli.console = Console(file=output, width=80, force_terminal=False)
    cli.ui = None
    cli._last_image_url = None
    cli._start_llm_request()
    cli._print_answer("回答内容")

    rendered = output.getvalue()
    assert "正在思考中" in rendered
    assert "…" not in rendered
    assert "Leon" not in rendered
    assert "• 回答内容" in rendered


def test_terminal_ui_uses_aurora_drift_palette(monkeypatch) -> None:
    class FakeOwner:
        llm_model = "fake-model"
        llm_provider_name = "fake-provider"
        session_id = "session-test"

    class FakeApplication:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.style = kwargs["style"]

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr("leon_agent.cli.Application", FakeApplication)
    ui = TerminalChatUI(FakeOwner())
    palette = dict(ui.app.style.style_rules)

    assert palette["message.assistant"] == "#DCEEFF"
    assert palette["message.marker.old"] == "#65E7B8"
    assert palette["message.tool"] == "#71869A"
    assert palette["message.separator"] == "#516579"
    assert palette["message.link"] == "underline #73B8FF"
    assert palette["status.running"] == "#59D7E7"
    assert palette["status.success"] == "#65E7B8"
    assert palette["status.error"] == "#FF8FB1"
    assert palette["status.warning"] == "#F5C26B"
    assert palette["status.cancel"] == "#FF8FB1"
    assert palette["status.background"] == "#73B8FF"
    assert palette["composer.line"] == "#15304A"
    assert palette["composer.prompt"] == "bold #F5C26B"


def test_terminal_ui_render_width_tracks_very_narrow_terminal(monkeypatch) -> None:
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
    monkeypatch.setattr(
        cli_module.shutil,
        "get_terminal_size",
        lambda fallback: SimpleNamespace(columns=18),
    )
    ui = TerminalChatUI(FakeOwner())

    assert ui._render_width() == 16


def test_terminal_ui_delivers_native_hyperlink_with_open_fallback(monkeypatch) -> None:
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
    url = (
        "https://comfyui.928886540.xyz/view?filename=k2_queen_marika_00004_.png"
        "&subfolder=2026-08-16%5Ck2_queen_marika&type=output"
    )

    ui.write_answer(f"1 张图片生成好了。\n\n- {url}")
    fragments = ui._output_fragments()
    rendered = "".join(
        fragment[1] for fragment in fragments if fragment[0] != "[ZeroWidthEscape]"
    )
    links = [
        fragment
        for fragment in fragments
        if "打开图片" in fragment[1]
    ]

    assert url not in rendered
    assert rendered.count("↗ 打开图片  ·  /open") == 1
    assert len(links) == 1
    assert ui._latest_image_url == url
    assert ("[ZeroWidthEscape]", f"\x1b]8;;{url}\x1b\\") in fragments
    assert ("[ZeroWidthEscape]", "\x1b]8;;\x1b\\") in fragments


def test_terminal_ui_keeps_multiple_image_links_distinct(monkeypatch) -> None:
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
    urls = [
        "https://images.example/first.png",
        "https://images.example/second.webp",
    ]

    ui.write_answer("生成完成\n\n" + "\n".join(f"- {url}" for url in urls))
    fragments = ui._output_fragments()
    rendered = "".join(
        fragment[1] for fragment in fragments if fragment[0] != "[ZeroWidthEscape]"
    )
    links = [
        fragment
        for fragment in fragments
        if "打开图片" in fragment[1]
    ]

    assert all(url not in rendered for url in urls)
    assert "↗ 打开图片 1/2" in rendered
    assert "↗ 打开图片 2/2  ·  /open" in rendered
    assert len(links) == 2
    assert ui._latest_image_url == urls[-1]
    for url in urls:
        assert ("[ZeroWidthEscape]", f"\x1b]8;;{url}\x1b\\") in fragments


def test_terminal_ui_does_not_invent_image_link_without_url(monkeypatch) -> None:
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

    ui.write_answer("1 张图片已经生成完成，正在同步结果…")
    fragments = ui._output_fragments()
    rendered = "".join(fragment[1] for fragment in fragments)

    assert "打开图片" not in rendered
    assert "/open" not in rendered
    assert ui._latest_image_url is None


def test_legacy_image_link_keeps_osc8_target_intact_at_narrow_width() -> None:
    output = StringIO()
    cli = LeonConsole.__new__(LeonConsole)
    cli.console = Console(
        file=output,
        width=12,
        force_terminal=True,
        legacy_windows=False,
    )
    cli.ui = None
    cli._last_image_url = None
    url = (
        "https://comfyui.928886540.xyz/view?filename=k2_queen_marika_00004_.png"
        "&subfolder=2026-08-16%5Ck2_queen_marika&type=output"
    )

    cli._print_answer(f"生成完成\n\n- {url}")

    rendered = output.getvalue()
    assert f";{url}\x1b\\" in rendered
    assert rendered.count(url) == 1
    assert "↗ 打开图片" in rendered
    assert "·  /open" in rendered


def test_terminal_ui_thinking_status_pulses_without_moving_ellipsis(monkeypatch) -> None:
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
    assert cli_module._THINKING_BEAM_SPEED == 2.0
    now = [100.0]
    monkeypatch.setattr(cli_module, "monotonic", lambda: now[0])
    ui = TerminalChatUI(FakeOwner())
    ui.busy = True
    ui._started_at = 28.0
    ui._set_status("正在思考中", animate=True)
    first_line = ui._status_line()
    first_fragments = ui._status_fragments()
    now[0] += 1 / 12
    second_line = ui._status_line()
    second_fragments = ui._status_fragments()

    assert first_line == "◦ 正在思考中 (1m 12s • esc 取消)"
    assert second_line == first_line
    assert "正在请求模型" not in first_line
    assert "..." not in first_line
    assert "".join(fragment[1] for fragment in first_fragments) == (
        "◦ 正在思考中 (1m 12s • esc 取消)"
    )
    assert "".join(fragment[1] for fragment in second_fragments) == (
        "◦ 正在思考中 (1m 12s • esc 取消)"
    )
    first_classes = [
        fragment[0]
        for fragment in first_fragments
        if fragment[0].startswith("class:status.pulse.")
    ]
    second_classes = [
        fragment[0]
        for fragment in second_fragments
        if fragment[0].startswith("class:status.pulse.")
    ]
    assert first_classes != second_classes
    observed_classes = set(first_classes + second_classes)
    assert "class:status.pulse.hot" in observed_classes
    assert "class:status.pulse.bright" in observed_classes
    assert "class:status.pulse.mid" in observed_classes
    assert "class:status.pulse.soft" in observed_classes
    assert first_classes[0] == second_classes[0] == "class:status.pulse.hot"
    weights = {
        "class:status.pulse.hot": 4,
        "class:status.pulse.bright": 3,
        "class:status.pulse.mid": 2,
        "class:status.pulse.soft": 1,
        "class:status.pulse.dim": 0,
    }
    first_center = sum(index * weights[style] for index, style in enumerate(first_classes))
    second_center = sum(index * weights[style] for index, style in enumerate(second_classes))
    assert second_center > first_center
    ui.busy = False
    ui._set_status(TerminalChatUI._IDLE_STATUS)
    assert ui._status_line() == ""


def test_windows_image_launcher_keeps_query_string_in_one_argument(monkeypatch) -> None:
    launched = []
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    monkeypatch.setattr(
        cli_module.subprocess,
        "Popen",
        lambda args, **kwargs: launched.append((args, kwargs)),
    )
    url = (
        "https://comfyui.example/view?filename=latest.png"
        "&subfolder=2026-08-17%5Cnsfw&type=output"
    )

    assert cli_module._launch_external_url(url) is True
    assert launched[0][0] == ["explorer.exe", url]


def test_legacy_open_last_image_is_a_keyboard_safe_fallback(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(
        cli_module,
        "_launch_external_url",
        lambda url: opened.append(url) or True,
    )
    cli = LeonConsole.__new__(LeonConsole)
    cli.console = Console(quiet=True)
    cli.ui = None
    cli._last_image_url = (
        "https://comfyui.example/view?filename=latest.png"
        "&subfolder=2026-08-17%5Cnsfw&type=output"
    )

    assert cli.open_last_image() is True
    assert opened == [cli._last_image_url]


def test_legacy_open_last_image_does_not_report_false_success(monkeypatch) -> None:
    def fail_to_launch(url):  # noqa: ANN001, ARG001
        raise OSError("browser launch failed")

    monkeypatch.setattr(cli_module, "_launch_external_url", fail_to_launch)
    output = StringIO()
    cli = LeonConsole.__new__(LeonConsole)
    cli.console = Console(file=output, width=100)
    cli.ui = None
    cli._last_image_url = "https://comfyui.example/view?filename=latest.png"

    assert cli.open_last_image() is False
    rendered = output.getvalue()
    assert "打开图片失败" in rendered
    assert "已交给系统浏览器" not in rendered


def test_legacy_prompt_uses_ascii_marker_for_non_unicode_console() -> None:
    assert _legacy_prompt_markup(SimpleNamespace(encoding="gbk")) == (
        "\n[bold yellow]>[/bold yellow]"
    )
    assert _legacy_prompt_markup(SimpleNamespace(encoding="utf-8")) == (
        "\n[bold yellow]»[/bold yellow]"
    )


def test_terminal_ui_switches_from_thinking_to_answering_on_first_delta(monkeypatch) -> None:
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
    cli = LeonConsole.__new__(LeonConsole)
    cli.ui = ui

    cli._on_event(SimpleNamespace(kind="turn_started", turn=1))
    assert ui.status_text == "正在思考中"
    assert ui._status_animated is True

    cli._on_event(SimpleNamespace(kind="assistant_delta"))
    assert ui.status_text == "正在回答"
    assert ui._status_animated is True
