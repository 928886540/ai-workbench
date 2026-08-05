"""Runtime settings loaded from environment variables."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central config for all projects in this monorepo."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_base_url: str = Field(default="https://api.openai.com/v1", alias="LLM_BASE_URL")
    llm_api_key: SecretStr = Field(default=SecretStr(""), alias="LLM_API_KEY")
    llm_model: str = Field(default="gpt-4.1-mini", alias="LLM_MODEL")
    llm_timeout_seconds: float = Field(default=60.0, alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=2, alias="LLM_MAX_RETRIES")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    def require_api_key(self) -> str:
        key = self.llm_api_key.get_secret_value().strip()
        if not key or key == "sk-your-key-here":
            raise ValueError(
                "LLM_API_KEY is missing. Copy .env.example to .env and set a real key."
            )
        return key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
