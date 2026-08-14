import pytest
from leon_agent.cli import LeonConsole, parse_args
from leon_agent.session import SessionStore
from rich.console import Console


class FailingAgent:
    def run(self, message, *, history):  # noqa: ANN001, ARG002
        raise RuntimeError("upstream failed")


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
