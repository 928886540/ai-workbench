"""Leon Agent-specific runtime settings."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
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
    default_modes: str = Field(default="k2_tifa_plus", alias="LEON_DEFAULT_IMAGE_MODES")
    http_timeout_seconds: float = Field(default=30.0, alias="LEON_HTTP_TIMEOUT_SECONDS")
    bridge_timeout_seconds: float = Field(default=20.0, alias="LEON_BRIDGE_TIMEOUT_SECONDS")
    api_token: SecretStr | None = Field(default=None, alias="LEON_API_TOKEN")
    system_prompt_file: Path | None = Field(
        default=None,
        alias="LEON_SYSTEM_PROMPT_FILE",
    )

    # Volink TTS. The key stays server-side: audio is proxied so the browser
    # never sees it.
    volink_api_key: SecretStr | None = Field(default=None, alias="VOLINK_API_KEY")
    volink_base_url: str = Field(
        default="https://api.volink.org/v1",
        alias="VOLINK_BASE_URL",
    )
    # 风韵少妇 / sensetime-sensenova-tts-v1
    volink_default_voice_id: str = Field(
        default="689334e84d3396ad1d28ee9e",
        alias="VOLINK_DEFAULT_VOICE_ID",
    )
    voice_clip_ttl_seconds: float = Field(default=3600.0, alias="LEON_VOICE_CLIP_TTL_SECONDS")
    voice_clip_max_count: int = Field(default=200, alias="LEON_VOICE_CLIP_MAX_COUNT")

    @field_validator("backend_url", "public_image_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("volink_base_url")
    @classmethod
    def normalize_volink_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def voice_enabled(self) -> bool:
        return bool(self.volink_api_key and self.volink_api_key.get_secret_value().strip())

    @property
    def active_plugin_dir(self) -> Path:
        candidate = self.plugin_dir or DEFAULT_PLUGIN_DIR
        return candidate.expanduser().resolve()

    @property
    def active_public_image_base_url(self) -> str:
        """Base URL used to build image links the user can actually open."""
        return self.public_image_base_url or self.backend_url

    def read_additional_system_prompt(self) -> str | None:
        """Read the optional UTF-8 text appended to Leon's system prompt."""
        if self.system_prompt_file is None:
            return None
        path = self.system_prompt_file.expanduser()
        if not path.is_absolute():
            path = REPO_ROOT / path
        path = path.resolve()
        if not path.is_file():
            raise ValueError(f"LEON_SYSTEM_PROMPT_FILE is not a file: {path}")
        try:
            content = path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"LEON_SYSTEM_PROMPT_FILE must be UTF-8 encoded: {path}"
            ) from exc
        except OSError as exc:
            raise ValueError(f"Cannot read LEON_SYSTEM_PROMPT_FILE: {path}") from exc
        if not content:
            raise ValueError(f"LEON_SYSTEM_PROMPT_FILE is empty: {path}")
        return content

    @property
    def default_mode_ids(self) -> list[str]:
        return [item.strip() for item in self.default_modes.split(",") if item.strip()]
