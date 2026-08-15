from pathlib import Path

from workbench_core.config import ENV_FILE, REPO_ROOT, Settings


def test_settings_env_file_is_bound_to_repository_root() -> None:
    configured_env_file = Path(Settings.model_config["env_file"])

    assert configured_env_file == ENV_FILE
    assert configured_env_file == REPO_ROOT / ".env"
    assert configured_env_file.is_absolute()


def test_env_source_settings() -> None:
    settings = Settings(
        LLM_SOURCE="env",
        LLM_BASE_URL="http://localhost:9/v1",
        LLM_API_KEY="sk-test",
        LLM_MODEL="test-model",
    )
    assert settings.profile == "env"
    assert settings.active_base_url == "http://localhost:9/v1"
    assert settings.active_model == "test-model"
    assert settings.require_api_key() == "sk-test"


def test_llm_request_defaults_are_safe_for_low_rpm_providers() -> None:
    settings = Settings(
        _env_file=None,
        LLM_SOURCE="env",
        LLM_BASE_URL="http://localhost:9/v1",
        LLM_API_KEY="sk-test",
        LLM_MODEL="test-model",
    )

    assert settings.llm_timeout_seconds == 30.0
    assert settings.llm_max_retries == 0


def test_toml_source_reads_active_codex_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '''
model_provider = "codex"
model = "active-model"

[model_providers.codex]
model = "provider-fallback-model"
base_url = "https://gateway.example/v1/"
experimental_bearer_token = "sk-test-token"
'''.strip(),
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,
        LLM_SOURCE="toml",
        CODEX_CONFIG_PATH=config_path,
    )

    assert settings.profile == "toml:codex"
    assert settings.active_base_url == "https://gateway.example/v1"
    assert settings.active_model == "active-model"
    assert settings.require_api_key() == "sk-test-token"
