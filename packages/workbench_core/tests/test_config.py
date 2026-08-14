from workbench_core.config import Settings


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
