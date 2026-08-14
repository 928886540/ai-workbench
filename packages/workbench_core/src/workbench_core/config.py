"""Runtime settings.

Default source is CC Switch local DB, so we don't hand-maintain provider secrets.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PrivateAttr, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from workbench_core.ccs import CCSProvider, resolve_provider

SourceName = Literal["ccs", "env"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_source: str = Field(default="ccs", alias="LLM_SOURCE")
    ccs_app: str = Field(default="codex", alias="CCS_APP")
    ccs_provider: str = Field(default="current", alias="CCS_PROVIDER")
    ccs_db_path: str | None = Field(default=None, alias="CCS_DB_PATH")

    llm_base_url: str | None = Field(default=None, alias="LLM_BASE_URL")
    llm_api_key: SecretStr | None = Field(default=None, alias="LLM_API_KEY")
    llm_model: str | None = Field(default=None, alias="LLM_MODEL")

    llm_timeout_seconds: float = Field(default=90.0, alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=2, alias="LLM_MAX_RETRIES")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    _ccs_provider: CCSProvider | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def normalize(self) -> Settings:
        source = (self.llm_source or "ccs").strip().lower()
        if source in {"ccs", "cc-switch", "ccswitch", "switch"}:
            self.llm_source = "ccs"
        elif source in {"env", "manual", "local"}:
            self.llm_source = "env"
        else:
            raise ValueError("LLM_SOURCE must be 'ccs' or 'env'")
        return self

    def load_ccs_provider(self) -> CCSProvider:
        if self._ccs_provider is not None:
            return self._ccs_provider
        name = self.ccs_provider
        use_current = name.strip().lower() in {"", "current", "当前", "*"}
        provider = resolve_provider(
            None if use_current else name,
            app_type=self.ccs_app,
            db_path=self.ccs_db_path,
            use_current=use_current,
        )
        self._ccs_provider = provider
        return provider

    @property
    def profile(self) -> str:
        if self.llm_source == "ccs":
            p = self.load_ccs_provider()
            return f"ccs:{p.name}"
        return "env"

    @property
    def active_base_url(self) -> str:
        if self.llm_source == "ccs":
            return self.load_ccs_provider().base_url
        if not self.llm_base_url:
            raise ValueError("LLM_BASE_URL required when LLM_SOURCE=env")
        return self.llm_base_url

    @property
    def active_model(self) -> str:
        if self.llm_source == "ccs":
            return self.load_ccs_provider().model
        if not self.llm_model:
            raise ValueError("LLM_MODEL required when LLM_SOURCE=env")
        return self.llm_model

    def require_api_key(self) -> str:
        if self.llm_source == "ccs":
            return self.load_ccs_provider().api_key
        if self.llm_api_key is None:
            raise ValueError("LLM_API_KEY required when LLM_SOURCE=env")
        key = self.llm_api_key.get_secret_value().strip()
        if not key or key.startswith("sk-your-"):
            raise ValueError("LLM_API_KEY missing/invalid for LLM_SOURCE=env")
        return key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
