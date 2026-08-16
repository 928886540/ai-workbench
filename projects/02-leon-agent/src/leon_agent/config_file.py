"""User-level Leon configuration shared by the CLI and Web Gateway.

The file intentionally keeps the copied Codex TOML at the top level so the
existing provider resolver can read it. Leon-owned environment-style values
live under ``[leon.env]`` and are injected before either composition root
creates ``LeonSettings`` or shared LLM ``Settings``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workbench_core.codex_toml import CodexTomlError, resolve_from_toml

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 uses tomli
    import tomli as tomllib  # type: ignore[no-redef]


REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG_PATH = Path.home() / ".leon" / "config.toml"
CONFIG_PATH_ENV = "LEON_CONFIG_FILE"
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FORBIDDEN_ENV_KEYS = frozenset({"PATH", "PATHEXT", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"})

# These are the values copied from the repository .env during `leon-config init`.
# The loader rejects unknown keys so this file cannot become an arbitrary
# process-environment injection surface.
MIGRATED_ENV_KEYS = (
    "LLM_SOURCE",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_RETRIES",
    "LOG_LEVEL",
    "LEON_BACKEND_URL",
    "LEON_PUBLIC_IMAGE_BASE_URL",
    "LEON_PLUGIN_DIR",
    "LEON_DEFAULT_IMAGE_MODES",
    "LEON_SESSION_DB",
    "LEON_API_TOKEN",
    "LEON_SYSTEM_PROMPT_FILE",
    "LEON_FILE_ROOTS",
    "LEON_HTTP_TIMEOUT_SECONDS",
    "LEON_BRIDGE_TIMEOUT_SECONDS",
    "VOLINK_API_KEY",
    "VOLINK_BASE_URL",
    "VOLINK_DEFAULT_VOICE_ID",
    "LEON_VOICE_CLIP_TTL_SECONDS",
    "LEON_VOICE_CLIP_MAX_COUNT",
    "LEON_ASR_BASE_URL",
    "LEON_ASR_TOKEN",
    "LEON_ASR_MODEL",
    "LEON_ASR_MAX_BYTES",
    "TAVILY_API_KEY",
    "TAVILY_BASE_URL",
    "TAVILY_TIMEOUT_SECONDS",
    "TAVILY_MAX_RESULTS",
)
ALLOWED_ENV_KEYS = frozenset((*MIGRATED_ENV_KEYS, "CODEX_CONFIG_PATH"))
LLM_ENV_DEFAULTS = {
    "LLM_TIMEOUT_SECONDS": "30",
    "LLM_MAX_RETRIES": "0",
    "LOG_LEVEL": "INFO",
}


class LeonConfigError(ValueError):
    """A user-facing configuration file error without secret values."""


@dataclass(frozen=True)
class LeonConfigFile:
    path: Path | None
    env: dict[str, str] = field(repr=False)


# Values injected by the previous call are tracked so tests, reloads, and a
# changed config path do not leave stale secrets in the process environment.
_injected_env: dict[str, tuple[str | None, str | None]] = {}


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Resolve the user config path, honoring an explicit test/deployment override."""
    if path is not None:
        return Path(path).expanduser()
    configured = os.environ.get(CONFIG_PATH_ENV, "").strip()
    if not configured:
        return DEFAULT_CONFIG_PATH
    configured_path = Path(configured).expanduser()
    if not configured_path.is_absolute():
        raise LeonConfigError(f"{CONFIG_PATH_ENV} must be an absolute path.")
    return configured_path


def _stringify_value(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise LeonConfigError(f"[leon.env] value for {key} must be a scalar.")


def _read_file(path: Path) -> LeonConfigFile:
    if not path.exists():
        return LeonConfigFile(path=None, env={})
    if not path.is_file():
        raise LeonConfigError(f"Leon config path is not a file: {path}")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LeonConfigError(f"Cannot parse Leon config file: {path}") from exc

    leon_block = data.get("leon", {})
    if leon_block is None:
        leon_block = {}
    if not isinstance(leon_block, Mapping):
        raise LeonConfigError("[leon] must be a TOML table.")
    env_block = leon_block.get("env", {})
    if env_block is None:
        env_block = {}
    if not isinstance(env_block, Mapping):
        raise LeonConfigError("[leon.env] must be a TOML table.")

    values: dict[str, str] = {}
    for raw_key, raw_value in env_block.items():
        if not isinstance(raw_key, str) or not ENV_KEY_PATTERN.fullmatch(raw_key):
            raise LeonConfigError("[leon.env] contains an invalid environment key.")
        key = raw_key.upper()
        if key in FORBIDDEN_ENV_KEYS:
            raise LeonConfigError(f"[leon.env] does not allow process path key {raw_key}.")
        if key not in ALLOWED_ENV_KEYS:
            raise LeonConfigError(f"[leon.env] does not allow unknown key {raw_key}.")
        if key in values:
            raise LeonConfigError(f"[leon.env] contains duplicate key {key}.")
        values[key] = _stringify_value(key, raw_value)
    return LeonConfigFile(path=path, env=values)


def load_config_file(path: str | Path | None = None) -> LeonConfigFile:
    """Read the optional user config without mutating process state."""
    return _read_file(resolve_config_path(path))


def _restore_injected_env() -> None:
    for key, (injected, previous) in list(_injected_env.items()):
        if os.environ.get(key) == injected:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
    _injected_env.clear()


def apply_config_file(
    path: str | Path | None = None,
    *,
    required: bool = True,
) -> LeonConfigFile:
    """Inject the user config; the copied TOML is Leon's sole provider source.

    ``required=False`` is only for isolated unit tests and migration probes.
    Production composition roots must fail instead of falling back to Codex or
    CC Switch when the user file has not been initialized.
    """
    _restore_injected_env()
    config = load_config_file(path)
    if config.path is None:
        if required:
            raise LeonConfigError(
                "Leon config is missing; run `leon-config init` once to create "
                f"{resolve_config_path(path)}."
            )
        return config
    try:
        resolve_from_toml(config.path)
    except CodexTomlError as exc:
        raise LeonConfigError(
            f"Leon config has no usable LLM provider: {config.path}."
        ) from exc
    values = dict(config.env)
    # Leon never follows CC Switch after bootstrap. These two values are
    # managed by the user file even when a stale process/.env value exists.
    values["LLM_SOURCE"] = "toml"
    values["CODEX_CONFIG_PATH"] = str(config.path)
    for key, value in LLM_ENV_DEFAULTS.items():
        values.setdefault(key, value)

    # The file is authoritative for every managed key. Remove ambient values
    # for omitted keys so neither a launcher nor the repository .env can alter
    # Leon after the one-time migration.
    for key in sorted(ALLOWED_ENV_KEYS):
        value = values.get(key)
        previous = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
        _injected_env[key] = (value, previous)
    return config


def _toml_string(value: str) -> str:
    # JSON string escaping is compatible with TOML basic strings and safely
    # handles Windows backslashes, quotes, and non-ASCII text.
    return json.dumps(value, ensure_ascii=False)


def _current_env_values() -> dict[str, str]:
    """Collect supported values from process env, then the repository .env."""
    try:
        from dotenv import dotenv_values
    except ImportError as exc:  # pragma: no cover - dependency is declared by the project
        raise LeonConfigError("python-dotenv is required for `leon-config init`.") from exc

    dotenv_path = REPO_ROOT / ".env"
    source_values = dotenv_values(dotenv_path) if dotenv_path.is_file() else {}
    values: dict[str, str] = {}
    for key in MIGRATED_ENV_KEYS:
        value = os.environ.get(key)
        if value is None:
            value = source_values.get(key)
        if value is not None:
            values[key] = str(value)
    values["LLM_SOURCE"] = "toml"
    for key, value in LLM_ENV_DEFAULTS.items():
        values.setdefault(key, value)
    values.setdefault("LEON_FILE_ROOTS", "{}")
    return values


def initialize_config(
    *,
    source_path: str | Path | None = None,
    destination_path: str | Path | None = None,
) -> Path:
    """Bootstrap Leon once from Codex TOML plus the repository environment."""
    source = (
        Path(source_path).expanduser()
        if source_path
        else Path.home() / ".codex" / "config.toml"
    )
    destination = resolve_config_path(destination_path)
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise LeonConfigError(f"Codex config file not found: {source}")
    if source == destination:
        raise LeonConfigError("Leon destination must differ from the Codex source file.")
    if destination.exists():
        raise LeonConfigError(
            f"Leon config already exists: {destination}; edit that file directly."
        )

    try:
        raw = source.read_bytes()
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise LeonConfigError("The source Codex config is not a valid UTF-8 TOML file.") from exc
    leon_block = parsed.get("leon")
    if leon_block is not None and not isinstance(leon_block, Mapping):
        raise LeonConfigError("The source Codex config already uses [leon] as a non-table value.")
    if isinstance(leon_block, Mapping) and "env" in leon_block:
        raise LeonConfigError(
            "The source Codex config already contains [leon.env]; remove it before copying."
        )
    try:
        resolve_from_toml(source)
    except CodexTomlError as exc:
        raise LeonConfigError("The source config has no usable LLM provider.") from exc

    destination.parent.mkdir(parents=True, exist_ok=True)

    text = raw.decode("utf-8").rstrip() + "\n\n[leon.env]\n"
    env_values = _current_env_values()
    env_values["CODEX_CONFIG_PATH"] = str(destination)
    lines = [text]
    lines.extend(f"{key} = {_toml_string(value)}\n" for key, value in env_values.items())
    payload = "".join(lines).encode("utf-8")

    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=".leon-config-",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize Leon's user-level config")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init", help="Copy Codex TOML and migrate Leon env values")
    init.add_argument("--source", help="Codex config path (default: ~/.codex/config.toml)")
    init.add_argument("--destination", help="Leon config path (default: ~/.leon/config.toml)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        destination = initialize_config(
            source_path=args.source,
            destination_path=args.destination,
        )
    except LeonConfigError as exc:
        print(f"Leon config initialization failed: {exc}")
        return 1
    print(f"Leon config ready: {destination}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
