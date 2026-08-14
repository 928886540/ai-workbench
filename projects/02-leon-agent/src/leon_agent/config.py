"""Leon Agent-specific runtime settings."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PLUGIN_DIR = (
    REPO_ROOT.parent
    / "ComfyUI-aki"
    / "ComfyUI-aki-v3"
    / "ComfyUI"
    / "app"
    / "ios"
    / "plugin"
    / "leon-image"
)


class LeonSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    backend_url: str = Field(
        default="http://192.168.8.100:8188",
        alias="LEON_BACKEND_URL",
    )
    public_image_base_url: str = Field(default="", alias="LEON_PUBLIC_IMAGE_BASE_URL")
    plugin_dir: Path | None = Field(default=None, alias="LEON_PLUGIN_DIR")
    session_db: Path = Field(
        default=REPO_ROOT / "data" / "leon-agent.db",
        alias="LEON_SESSION_DB",
    )
    default_modes: str = Field(default="k2_tifa", alias="LEON_DEFAULT_IMAGE_MODES")
    http_timeout_seconds: float = Field(default=30.0, alias="LEON_HTTP_TIMEOUT_SECONDS")
    bridge_timeout_seconds: float = Field(default=20.0, alias="LEON_BRIDGE_TIMEOUT_SECONDS")

    @field_validator("backend_url", "public_image_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def active_plugin_dir(self) -> Path:
        candidate = self.plugin_dir or DEFAULT_PLUGIN_DIR
        return candidate.expanduser().resolve()

    @property
    def active_public_image_base_url(self) -> str:
        """Base URL used to build image links the user can actually open."""
        return self.public_image_base_url or self.backend_url

    @property
    def default_mode_ids(self) -> list[str]:
        return [item.strip() for item in self.default_modes.split(",") if item.strip()]
