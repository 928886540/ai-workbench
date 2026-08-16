from pathlib import Path

import pytest
from leon_agent.config import LeonSettings


def settings_with_prompt_file(path: Path) -> LeonSettings:
    return LeonSettings(_env_file=None, LEON_SYSTEM_PROMPT_FILE=path)


def test_reads_utf8_additional_system_prompt(tmp_path: Path) -> None:
    prompt_file = tmp_path / "preset.txt"
    prompt_file.write_text("第一行\n第二行\n", encoding="utf-8")

    settings = settings_with_prompt_file(prompt_file)

    assert settings.read_additional_system_prompt() == "第一行\n第二行"


def test_missing_system_prompt_file_fails_clearly(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"

    with pytest.raises(ValueError, match="LEON_SYSTEM_PROMPT_FILE is not a file"):
        settings_with_prompt_file(missing).read_additional_system_prompt()


def test_non_utf8_system_prompt_file_fails_clearly(tmp_path: Path) -> None:
    prompt_file = tmp_path / "gbk.txt"
    prompt_file.write_bytes("中文预设".encode("gbk"))

    with pytest.raises(ValueError, match="LEON_SYSTEM_PROMPT_FILE must be UTF-8"):
        settings_with_prompt_file(prompt_file).read_additional_system_prompt()


def test_empty_system_prompt_file_fails_clearly(tmp_path: Path) -> None:
    prompt_file = tmp_path / "empty.txt"
    prompt_file.write_text(" \n", encoding="utf-8")

    with pytest.raises(ValueError, match="LEON_SYSTEM_PROMPT_FILE is empty"):
        settings_with_prompt_file(prompt_file).read_additional_system_prompt()
