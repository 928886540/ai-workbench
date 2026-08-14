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
