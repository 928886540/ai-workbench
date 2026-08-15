from concurrent.futures import CancelledError
from threading import Event
from types import SimpleNamespace

import pytest
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


def test_custom_model_override_is_sent_in_request(monkeypatch) -> None:  # noqa: ANN001
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)
            message = SimpleNamespace(content="ok", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeOpenAI:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("workbench_core.llm.OpenAI", FakeOpenAI)
    settings = Settings(
        LLM_SOURCE="env",
        LLM_BASE_URL="https://new-api.abrdns.com/v1",
        LLM_API_KEY="sk-test",
        LLM_MODEL="default-model",
    )
    client = LLMClient(settings, model_override="DeepSeek-V4-Pro")

    client.chat_turn([{"role": "user", "content": "hello"}])

    assert captured["model"] == "DeepSeek-V4-Pro"


def test_client_passes_timeout_and_retry_policy_to_openai(monkeypatch) -> None:  # noqa: ANN001
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)

    monkeypatch.setattr("workbench_core.llm.OpenAI", FakeOpenAI)
    settings = Settings(
        _env_file=None,
        LLM_SOURCE="env",
        LLM_BASE_URL="https://provider.example/v1",
        LLM_API_KEY="sk-test",
        LLM_MODEL="default-model",
        LLM_TIMEOUT_SECONDS=17,
        LLM_MAX_RETRIES=0,
    )

    LLMClient(settings)

    assert captured["timeout"] == 17
    assert captured["max_retries"] == 0


def test_list_models_uses_active_provider_catalog(monkeypatch) -> None:  # noqa: ANN001
    class FakeModels:
        def list(self):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(id="z-model"),
                    SimpleNamespace(id="A-model"),
                    SimpleNamespace(id="z-model"),
                ]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.models = FakeModels()

    monkeypatch.setattr("workbench_core.llm.OpenAI", FakeOpenAI)
    settings = Settings(
        LLM_SOURCE="env",
        LLM_BASE_URL="https://provider.example/v1",
        LLM_API_KEY="sk-test",
        LLM_MODEL="default-model",
    )

    assert LLMClient(settings).list_models() == ["A-model", "z-model"]


def test_chat_turn_stops_before_provider_when_cancelled(monkeypatch) -> None:  # noqa: ANN001
    calls = 0

    class FakeCompletions:
        def create(self, **kwargs):  # noqa: ANN003, ARG002
            nonlocal calls
            calls += 1
            raise AssertionError("cancelled request must not reach the provider")

    class FakeOpenAI:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("workbench_core.llm.OpenAI", FakeOpenAI)
    settings = Settings(
        _env_file=None,
        LLM_SOURCE="env",
        LLM_BASE_URL="https://provider.example/v1",
        LLM_API_KEY="sk-test",
        LLM_MODEL="default-model",
    )
    cancel_event = Event()
    cancel_event.set()

    with pytest.raises(CancelledError):
        LLMClient(settings).chat_turn(
            [{"role": "user", "content": "hello"}],
            cancel_event=cancel_event,
        )

    assert calls == 0


def test_chat_turn_discards_response_when_cancelled_after_provider_returns(monkeypatch) -> None:  # noqa: ANN001
    cancel_event = Event()

    class FakeCompletions:
        def create(self, **kwargs):  # noqa: ANN003, ARG002
            cancel_event.set()
            message = SimpleNamespace(content="late", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeOpenAI:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("workbench_core.llm.OpenAI", FakeOpenAI)
    settings = Settings(
        _env_file=None,
        LLM_SOURCE="env",
        LLM_BASE_URL="https://provider.example/v1",
        LLM_API_KEY="sk-test",
        LLM_MODEL="default-model",
    )

    with pytest.raises(CancelledError):
        LLMClient(settings).chat_turn(
            [{"role": "user", "content": "hello"}],
            cancel_event=cancel_event,
        )
