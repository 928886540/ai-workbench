from workbench_core.config import Settings


def test_settings_defaults() -> None:
    settings = Settings(
        LLM_API_KEY="sk-test",
        LLM_BASE_URL="http://localhost:9/v1",
        LLM_MODEL="test-model",
    )
    assert settings.llm_model == "test-model"
    assert settings.require_api_key() == "sk-test"
