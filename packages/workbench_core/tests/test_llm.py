from workbench_core.config import Settings
from workbench_core.llm import LLMClient


def test_llm_client_model_override() -> None:
    settings = Settings(
        LLM_SOURCE="env",
        LLM_BASE_URL="http://localhost:9/v1",
        LLM_API_KEY="sk-test",
        LLM_MODEL="default-model",
    )

    client = LLMClient(settings, model_override="selected-model")

    assert client.model == "selected-model"
