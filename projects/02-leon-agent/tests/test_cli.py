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

    assert cli.process("这次会失败") is False
    assert cli.store.load_messages(cli.session_id) == []


def test_switch_model_accepts_custom_model_id(tmp_path) -> None:  # noqa: ANN001
    cli = LeonConsole.__new__(LeonConsole)
    cli.store = SessionStore(tmp_path / "leon.db")
    cli.session_id = cli.store.create_session()
    cli.console = Console(quiet=True)
    cli.model_selection = None

    class FakeSettings:
        profile = "toml:codex"

    cli._resolve_llm_settings = lambda: FakeSettings()  # type: ignore[method-assign]

    def fake_create_agent():
        cli.llm_model = cli.model_selection[1]
        cli.llm_profile = cli.model_selection[0]
        return object()

    cli._create_agent = fake_create_agent  # type: ignore[method-assign]

    cli.switch_model("DeepSeek-V4-Pro")

    assert cli.store.get_model_selection(cli.session_id) == (
        "toml:codex",
        "DeepSeek-V4-Pro",
    )
    assert cli.llm_model == "DeepSeek-V4-Pro"
