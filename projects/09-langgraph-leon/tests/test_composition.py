from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from leon_agent.config import LeonSettings
from leon_agent.config_file import apply_config_file
from leon_agent.memory import MemoryService, MemoryStore, memory_turn
from leon_framework import composition
from workbench_core.config import Settings, reset_settings_cache


@pytest.fixture(autouse=True)
def restore_applied_leon_config(tmp_path: Path):  # noqa: ANN201
    yield
    apply_config_file(tmp_path / "missing.toml", required=False)
    reset_settings_cache()


def test_build_chat_model_uses_resolved_settings_without_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(composition, "ChatOpenAI", FakeChatOpenAI)
    settings = Settings(
        _env_file=None,
        LLM_SOURCE="env",
        LLM_BASE_URL="https://provider.test/v1",
        LLM_API_KEY="test-key",
        LLM_MODEL="test-model",
        LLM_TIMEOUT_SECONDS=12,
        LLM_MAX_RETRIES=2,
    )

    composition.build_chat_model(settings)

    assert captured == {
        "api_key": "test-key",
        "base_url": "https://provider.test/v1",
        "model": "test-model",
        "temperature": 0,
        "max_retries": 2,
        "timeout": 12.0,
    }


def test_build_tool_registry_exposes_only_milestone_one_tools(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("framework edition\n", encoding="utf-8")
    settings = LeonSettings(
        _env_file=None,
        LEON_FILE_ROOTS={"workspace": tmp_path},
        TAVILY_API_KEY="test-search-key",
        TAVILY_FALLBACK_API_KEY="",
        TAVILY_FALLBACK_BASE_URL="",
    )

    registry = composition.build_tool_registry(settings)

    assert registry.names == ["file_search", "read_file", "web_search"]
    assert "list_files" not in registry.names
    assert "create_file" not in registry.names
    assert "generate_images" not in registry.names
    result = registry.execute(
        "read_file",
        {
            "root_id": "workspace",
            "relative_path": "sample.txt",
            "start_line": 1,
            "max_lines": 20,
        },
    )
    assert result["ok"] is True
    assert "framework edition" in result["content"]


def test_build_tool_registry_reuses_memory_tools_and_consent_policy(tmp_path: Path) -> None:
    settings = LeonSettings(
        _env_file=None,
        LEON_FILE_ROOTS={},
        TAVILY_API_KEY="",
        TAVILY_FALLBACK_API_KEY="",
        TAVILY_FALLBACK_BASE_URL="",
    )
    service = MemoryService(
        MemoryStore(tmp_path / "memory.db"),
        session_id="framework-thread",
    )
    registry = composition.build_tool_registry(settings, memory_service=service)

    assert registry.names == ["memory_get", "memory_upsert", "memory_delete"]
    without_consent = registry.execute(
        "memory_upsert",
        {"key": "profile.style", "value": {"value": "concise"}},
    )
    with memory_turn("remember this preference"):
        saved = registry.execute(
            "memory_upsert",
            {"key": "profile.style", "value": {"value": "concise"}},
        )
        second = registry.execute(
            "memory_upsert",
            {"key": "profile.style", "value": {"value": "verbose"}},
        )

    assert without_consent["error_code"] == "explicit_consent_required"
    assert saved["ok"] is True
    assert second["error_code"] == "write_limit_reached"
    loaded = registry.execute("memory_get", {"key": "profile.style"})
    assert loaded["memories"][0]["value"] == {"value": "concise"}


def test_load_framework_settings_is_pinned_to_leon_toml(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    path = tmp_path / "config.toml"
    file_roots = json.dumps({"docs": str(root)})
    path.write_text(
        "\n".join(
            [
                'model_provider = "test"',
                'model = "framework-model"',
                "",
                "[model_providers.test]",
                'base_url = "http://127.0.0.1:9/v1"',
                'experimental_bearer_token = "test-provider-key"',
                "",
                "[leon.env]",
                f"LEON_FILE_ROOTS = {json.dumps(file_roots)}",
            ]
        ),
        encoding="utf-8",
    )

    config_file, llm_settings, leon_settings = composition.load_framework_settings(path)

    assert config_file.path == path
    assert llm_settings.llm_source == "toml"
    assert llm_settings.codex_config_path == path
    assert llm_settings.active_model == "framework-model"
    assert leon_settings.file_roots == {"docs": root}
