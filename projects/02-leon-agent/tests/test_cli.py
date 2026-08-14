import pytest
from leon_agent.cli import parse_args


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
