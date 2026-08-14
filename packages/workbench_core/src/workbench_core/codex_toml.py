"""Read the active LLM provider from Codex' own `~/.codex/config.toml`.

CC Switch writes its active Codex provider into this file, so reading it directly
means the agent always follows whatever provider is currently switched on, without
depending on a possibly-stale secondary local DB.

Layout looked up:

    model_provider = "codex"
    model = "DeepSeek-V4-Flash-0731"

    [model_providers.codex]
    name = "codex"
    model = "DeepSeek-V4-Flash-0731"
    requires_openai_auth = true
    base_url = "https://new-api.abrdns.com/v1"
    wire_api = "responses"
    experimental_bearer_token = "sk-..."
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - py3.10 falls back to tomli
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_CONFIG_PATH = Path.home() / ".codex" / "config.toml"


class CodexTomlError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexProvider:
    provider: str
    base_url: str
    api_key: str
    model: str
    config_path: Path | None = None

    def summary(self) -> str:
        key = self.api_key
        masked = f"{key[:4]}...{key[-4:]}" if len(key) > 10 else "***"
        return (
            f"[toml] provider={self.provider} | model={self.model} | "
            f"base={self.base_url} | key={masked}"
        )


def _active_model(config_model: object, provider_block: dict[str, Any]) -> str:
    if isinstance(config_model, str) and config_model.strip():
        return config_model.strip()
    provider_model = provider_block.get("model")
    if isinstance(provider_model, str) and provider_model.strip():
        return provider_model.strip()
    return "unknown"


def resolve_from_toml(
    config_path: str | Path | None = None,
    *,
    provider_name: str | None = None,
) -> CodexProvider:
    """Resolve the active provider config straight from `~/.codex/config.toml`.

    ``provider_name`` overrides the top-level ``model_provider`` key; otherwise the
    currently active provider is used, matching CC Switch's switch-on behavior.
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise CodexTomlError(f"Codex config.toml not found: {path}")

    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CodexTomlError(f"Failed to parse Codex config.toml at {path}: {exc}") from exc

    providers = data.get("model_providers")
    if not isinstance(providers, dict):
        raise CodexTomlError(f"config.toml has no [model_providers] table: {path}")

    name = (provider_name or data.get("model_provider") or "").strip()
    if not name:
        raise CodexTomlError(f"config.toml sets no model_provider: {path}")

    block = providers.get(name)
    if not isinstance(block, dict):
        raise CodexTomlError(
            f"config.toml has no [model_providers.{name}] section: {path}"
        )

    base_url = str(block.get("base_url") or "").strip().rstrip("/")
    api_key = str(
        block.get("experimental_bearer_token")
        or block.get("api_key")
        or block.get("OPENAI_API_KEY")
        or ""
    ).strip()

    if not base_url:
        raise CodexTomlError(f"config.toml [model_providers.{name}] missing base_url: {path}")
    if not api_key:
        raise CodexTomlError(
            f"config.toml [model_providers.{name}] missing experimental_bearer_token: {path}"
        )

    return CodexProvider(
        provider=name,
        base_url=base_url,
        api_key=api_key,
        model=_active_model(data.get("model"), block),
        config_path=path,
    )
