from types import SimpleNamespace

from langchain_lab import model as model_module


def test_chat_model_maps_settings_without_passing_zero_timeout(monkeypatch) -> None:
    calls = []

    class StubChatOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(model_module, "ChatOpenAI", StubChatOpenAI)
    settings = SimpleNamespace(
        active_base_url="https://provider.example/v1",
        active_model="lab-model",
        llm_max_retries=1,
        llm_timeout_seconds=0,
        require_api_key=lambda: "sk-secret",
    )

    model_module.build_chat_model(settings)

    assert calls[0]["model"] == "lab-model"
    assert calls[0]["api_key"] == "sk-secret"
    assert "timeout" not in calls[0]
