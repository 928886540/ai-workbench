import pytest
from leon_agent.image_modes import (
    DEFAULT_NSFW_MODE_ID,
    format_mode_catalog,
    parse_nsfw_command,
)

MODES = [
    {"id": "k2_queen_marika"},
    {"id": "k2_tifa"},
    {"id": "k2_tifa_plus"},
    {"id": "nsfw_realcore"},
]


def test_nsfw_defaults_to_marika() -> None:
    command = parse_nsfw_command("/nsfw 生成一张雨夜图", MODES)

    assert command is not None
    assert command.workflow_id == DEFAULT_NSFW_MODE_ID
    assert command.mode_name == "玛莉卡"
    assert command.source_text == "生成一张雨夜图"


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ("tifa-plus", "k2_tifa_plus"),
        ("蒂法增强", "k2_tifa_plus"),
        ("玛莉卡", "k2_queen_marika"),
        ("realcore", "nsfw_realcore"),
    ],
)
def test_nsfw_resolves_english_and_chinese_mode_aliases(
    selection: str,
    expected: str,
) -> None:
    command = parse_nsfw_command(
        f"/nsfw --model {selection} 原样描述",
        MODES,
    )

    assert command is not None
    assert command.workflow_id == expected
    assert command.source_text == "原样描述"


def test_nsfw_without_prompt_requests_mode_catalog() -> None:
    assert parse_nsfw_command("/nsfw", MODES) is None
    catalog = format_mode_catalog(MODES)

    assert "玛莉卡" in catalog
    assert "k2_queen_marika" in catalog
    assert "蒂法增强" in catalog


def test_nsfw_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="未知生图模式"):
        parse_nsfw_command("/nsfw --model 不存在 原样描述", MODES)
