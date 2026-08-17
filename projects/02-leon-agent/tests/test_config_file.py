from __future__ import annotations

from pathlib import Path

import pytest
from leon_agent import config_file
from workbench_core.config import Settings

CODEX_FIXTURE = """\
model_provider = "test"
model = "test-model"

[model_providers.test]
name = "test"
base_url = "http://127.0.0.1:9/v1"
experimental_bearer_token = "test-provider-key"
"""


def _clear_applied_config(tmp_path: Path) -> None:
    config_file.apply_config_file(tmp_path / "missing.toml", required=False)


def test_apply_config_file_requires_user_config(tmp_path: Path) -> None:
    with pytest.raises(config_file.LeonConfigError, match="leon-config init"):
        config_file.apply_config_file(tmp_path / "missing.toml")


def test_apply_config_file_rejects_config_without_provider(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[leon.env]\nLLM_SOURCE = 'toml'\n", encoding="utf-8")

    with pytest.raises(config_file.LeonConfigError, match="usable LLM provider"):
        config_file.apply_config_file(path)


def test_config_file_environment_override_must_be_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(config_file.CONFIG_PATH_ENV, "relative/config.toml")

    with pytest.raises(config_file.LeonConfigError, match="absolute"):
        config_file.resolve_config_path()


def test_apply_config_file_uses_user_toml_instead_of_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        CODEX_FIXTURE
        + """

[leon.env]
TAVILY_API_KEY = "file-search-key"
LEON_BACKEND_URL = "http://file-backend"
TAVILY_MAX_RESULTS = 7
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEON_BACKEND_URL", "http://process-backend")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_CONFIG_PATH", raising=False)

    loaded = config_file.apply_config_file(path)

    assert loaded.path == path
    assert loaded.env["TAVILY_MAX_RESULTS"] == "7"
    assert config_file.os.environ["TAVILY_API_KEY"] == "file-search-key"
    assert config_file.os.environ["LEON_BACKEND_URL"] == "http://file-backend"
    assert config_file.os.environ["CODEX_CONFIG_PATH"] == str(path)
    _clear_applied_config(tmp_path)


def test_apply_config_file_pins_provider_to_leon_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(CODEX_FIXTURE + "\n[leon.env]\nLLM_SOURCE = 'ccs'\n", encoding="utf-8")
    monkeypatch.setenv("LLM_SOURCE", "ccs")
    monkeypatch.setenv("CODEX_CONFIG_PATH", str(tmp_path / "codex.toml"))

    config_file.apply_config_file(path)

    assert config_file.os.environ["LLM_SOURCE"] == "toml"
    assert config_file.os.environ["CODEX_CONFIG_PATH"] == str(path)
    assert config_file.os.environ["LLM_TIMEOUT_SECONDS"] == "0"
    settings = Settings()
    assert settings.llm_source == "toml"
    assert settings.codex_config_path == path
    assert settings.llm_timeout_seconds == 0.0
    _clear_applied_config(tmp_path)


def test_config_file_rejects_process_path_injection(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[leon.env]\nPYTHONPATH = 'unsafe'\n", encoding="utf-8")

    with pytest.raises(config_file.LeonConfigError, match="does not allow"):
        config_file.load_config_file(path)


def test_config_file_rejects_unknown_environment_keys_without_secret_repr(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[leon.env]\nUNKNOWN_SETTING = "super-secret-value"\n',
        encoding="utf-8",
    )

    with pytest.raises(config_file.LeonConfigError, match="unknown key"):
        config_file.load_config_file(path)

    loaded = config_file.LeonConfigFile(path=path, env={"TAVILY_API_KEY": "super-secret-value"})
    assert "super-secret-value" not in repr(loaded)


def test_apply_config_file_overrides_case_insensitive_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        CODEX_FIXTURE
        + '\n[leon.env]\nLEON_BACKEND_URL = "http://file-backend"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("LEON_BACKEND_URL", raising=False)
    monkeypatch.setenv("leon_backend_url", "http://process-backend")

    config_file.apply_config_file(path)

    assert config_file.os.environ.get("leon_backend_url") == "http://file-backend"
    assert config_file.os.environ.get("LEON_BACKEND_URL") == "http://file-backend"
    _clear_applied_config(tmp_path)


def test_apply_config_file_removes_ambient_managed_key_when_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(CODEX_FIXTURE + "\n[leon.env]\n", encoding="utf-8")
    monkeypatch.setenv("TAVILY_API_KEY", "ambient-key")

    config_file.apply_config_file(path)

    assert "TAVILY_API_KEY" not in config_file.os.environ
    _clear_applied_config(tmp_path)
    assert config_file.os.environ["TAVILY_API_KEY"] == "ambient-key"


def test_apply_config_file_clears_values_omitted_by_a_reloaded_user_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.toml"
    second = tmp_path / "second.toml"
    first.write_text(
        CODEX_FIXTURE + '\n[leon.env]\nTAVILY_API_KEY = "first-key"\n',
        encoding="utf-8",
    )
    second.write_text(CODEX_FIXTURE + "\n[leon.env]\n", encoding="utf-8")
    monkeypatch.setenv("TAVILY_API_KEY", "ambient-key")

    config_file.apply_config_file(first)
    assert config_file.os.environ["TAVILY_API_KEY"] == "first-key"

    config_file.apply_config_file(second)
    assert "TAVILY_API_KEY" not in config_file.os.environ

    _clear_applied_config(tmp_path)
    assert config_file.os.environ["TAVILY_API_KEY"] == "ambient-key"


def test_initialize_config_copies_provider_and_migrates_env_without_echoing_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "codex.toml"
    destination = tmp_path / ".leon" / "config.toml"
    source.write_text(CODEX_FIXTURE, encoding="utf-8")
    monkeypatch.setattr(
        config_file,
        "_current_env_values",
        lambda: {
            "LLM_SOURCE": "toml",
            "LLM_TIMEOUT_SECONDS": "0",
            "TAVILY_API_KEY": "test-search-key",
            "LEON_FILE_ROOTS": "{}",
        },
    )

    created = config_file.initialize_config(
        source_path=source,
        destination_path=destination,
    )

    assert created == destination
    parsed = config_file.tomllib.loads(destination.read_text(encoding="utf-8"))
    assert parsed["model_provider"] == "test"
    assert parsed["model_providers"]["test"]["experimental_bearer_token"] == (
        "test-provider-key"
    )
    assert parsed["leon"]["env"]["TAVILY_API_KEY"] == "test-search-key"
    assert parsed["leon"]["env"]["CODEX_CONFIG_PATH"] == str(destination)
    assert parsed["leon"]["env"]["LLM_TIMEOUT_SECONDS"] == "0"

    with pytest.raises(config_file.LeonConfigError, match="edit that file directly"):
        config_file.initialize_config(
            source_path=source,
            destination_path=destination,
        )


def test_initialize_config_rejects_existing_leon_env_table(tmp_path: Path) -> None:
    source = tmp_path / "codex.toml"
    destination = tmp_path / ".leon" / "config.toml"
    source.write_text(CODEX_FIXTURE + "\n[leon.env]\nOLD = 'value'\n", encoding="utf-8")

    with pytest.raises(config_file.LeonConfigError, match="already contains"):
        config_file.initialize_config(source_path=source, destination_path=destination)
