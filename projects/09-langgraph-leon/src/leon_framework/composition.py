"""Composition root for the framework edition of Leon."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI
from leon_agent.config import LeonSettings
from leon_agent.config_file import LeonConfigFile, apply_config_file
from leon_agent.file_tools import create_file_search_service, create_file_tools
from leon_agent.memory import MemoryService, MemoryStore
from leon_agent.memory.tools import create_memory_tools
from leon_agent.search import create_search_service
from leon_agent.tools import create_web_search_tool
from workbench_core.agent import AgentTool, ToolRegistry
from workbench_core.config import Settings, get_settings, reset_settings_cache

FILE_TOOL_NAMES = frozenset({"file_search", "read_file"})


@dataclass(frozen=True)
class FrameworkComponents:
    """Resolved provider, model, and the small Milestone 1 tool surface."""

    config_file: LeonConfigFile
    llm_settings: Settings
    leon_settings: LeonSettings
    model: Any
    registry: ToolRegistry
    memory_service: MemoryService | None


def load_framework_settings(
    config_path: str | Path | None = None,
) -> tuple[LeonConfigFile, Settings, LeonSettings]:
    """Load Leon's private config without falling back to CCS or repository env."""

    config_file = apply_config_file(config_path)
    reset_settings_cache()
    return config_file, get_settings(), LeonSettings()


def build_chat_model(settings: Settings) -> ChatOpenAI:
    """Construct LangChain's OpenAI-compatible chat model from Leon settings."""

    kwargs: dict[str, Any] = {
        "api_key": settings.require_api_key(),
        "base_url": settings.active_base_url,
        "model": settings.active_model,
        "temperature": 0,
        "max_retries": settings.llm_max_retries,
    }
    if settings.llm_timeout_seconds > 0:
        kwargs["timeout"] = settings.llm_timeout_seconds
    return ChatOpenAI(**kwargs)


def build_tool_registry(
    settings: LeonSettings,
    *,
    memory_service: MemoryService | None = None,
) -> ToolRegistry:
    """Expose only File Search/Read and Web Search through canonical factories."""

    tools: list[AgentTool] = []
    file_service = create_file_search_service(settings.file_roots)
    if file_service is not None:
        tools.extend(
            tool
            for tool in create_file_tools(file_service)
            if tool.name in FILE_TOOL_NAMES
        )

    search_service = create_search_service(
        api_key=(
            settings.tavily_api_key.get_secret_value()
            if settings.tavily_api_key is not None
            else None
        ),
        base_url=settings.tavily_base_url,
        timeout_seconds=settings.tavily_timeout_seconds,
        max_results=settings.tavily_max_results,
        fallback_api_key=(
            settings.tavily_fallback_api_key.get_secret_value()
            if settings.tavily_fallback_api_key is not None
            else None
        ),
        fallback_base_url=settings.tavily_fallback_base_url,
    )
    if search_service is not None:
        tools.append(create_web_search_tool(search_service))
    if memory_service is not None:
        tools.extend(create_memory_tools(memory_service))
    return ToolRegistry(tools)


def build_framework_components(
    config_path: str | Path | None = None,
    *,
    session_id: str | None = None,
    enable_memory: bool = False,
) -> FrameworkComponents:
    """Build the real Framework Edition without making a provider request."""

    config_file, llm_settings, leon_settings = load_framework_settings(config_path)
    if enable_memory and not (session_id or "").strip():
        raise ValueError("session_id is required when memory is enabled")
    memory_service = (
        MemoryService(
            MemoryStore(leon_settings.session_db),
            session_id=str(session_id),
        )
        if enable_memory
        else None
    )
    return FrameworkComponents(
        config_file=config_file,
        llm_settings=llm_settings,
        leon_settings=leon_settings,
        model=build_chat_model(llm_settings),
        registry=build_tool_registry(
            leon_settings,
            memory_service=memory_service,
        ),
        memory_service=memory_service,
    )
