"""Read LLM provider configs from local CC Switch database.

CC Switch stores providers in:
  %USERPROFILE%\\.cc-switch\\cc-switch.db

There is no dedicated MCP for config export, so we read SQLite directly.
Default app_type is `codex` because those providers are OpenAI-compatible.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".cc-switch" / "cc-switch.db"


class CCSError(RuntimeError):
    pass


@dataclass(frozen=True)
class CCSProvider:
    id: str
    app_type: str
    name: str
    is_current: bool
    base_url: str
    api_key: str
    model: str
    notes: str | None = None

    def summary(self) -> str:
        key = self.api_key
        masked = f"{key[:4]}...{key[-4:]}" if len(key) > 10 else "***"
        flag = "*" if self.is_current else " "
        return (
            f"[{flag}] {self.app_type:6} | {self.name} | model={self.model} | "
            f"base={self.base_url} | key={masked}"
        )


def _parse_auth(raw: object) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {"OPENAI_API_KEY": raw}
    return {}


def _extract_api_key(auth: dict, env: dict | None = None) -> str:
    env = env or {}
    candidates = [
        auth.get("OPENAI_API_KEY"),
        auth.get("api_key"),
        auth.get("apiKey"),
        env.get("OPENAI_API_KEY"),
        env.get("ANTHROPIC_AUTH_TOKEN"),
        env.get("ANTHROPIC_API_KEY"),
    ]
    tokens = auth.get("tokens")
    if isinstance(tokens, dict):
        candidates.append(tokens.get("access_token"))
    for item in candidates:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ""


def _extract_from_codex_config(config_text: str) -> tuple[str, str]:
    base_url = ""
    model = ""
    if not config_text:
        return base_url, model
    m = re.search(r'base_url\s*=\s*"([^"]+)"', config_text)
    if m:
        base_url = m.group(1).strip()
    m = re.search(r'^model\s*=\s*"([^"]+)"', config_text, re.M)
    if m:
        model = m.group(1).strip()
    return base_url, model


def _normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return url
    # OpenAI client expects .../v1
    if url.endswith("/v1"):
        return url
    # Anthropic-style roots sometimes omit /v1. Keep the configured root unchanged because
    # a compatible gateway may expose its OpenAI endpoint there.
    return url


def _provider_from_row(row: sqlite3.Row) -> CCSProvider | None:
    try:
        cfg = json.loads(row["settings_config"] or "{}")
    except json.JSONDecodeError:
        return None

    app_type = row["app_type"]
    base_url = ""
    model = ""
    api_key = ""

    if app_type == "codex":
        auth = _parse_auth(cfg.get("auth"))
        conf = cfg.get("config") or ""
        base_url, model = _extract_from_codex_config(conf)
        api_key = _extract_api_key(auth)
    else:
        env = cfg.get("env") or {}
        if not isinstance(env, dict):
            env = {}
        base_url = str(
            env.get("OPENAI_BASE_URL")
            or env.get("ANTHROPIC_BASE_URL")
            or cfg.get("base_url")
            or ""
        ).strip()
        model = str(
            cfg.get("model")
            or env.get("ANTHROPIC_MODEL")
            or env.get("OPENAI_MODEL")
            or ""
        ).strip()
        api_key = _extract_api_key({}, env)
        # For OpenAI-compatible usage via workbench, prefer /v1 if missing
        if base_url and not base_url.rstrip("/").endswith("/v1"):
            # keep as-is for anthropic; caller can still use if gateway supports openai path
            pass

    base_url = _normalize_base_url(base_url)
    if not base_url or not api_key:
        return None

    return CCSProvider(
        id=row["id"],
        app_type=app_type,
        name=row["name"],
        is_current=bool(row["is_current"]),
        base_url=base_url,
        api_key=api_key,
        model=model or "unknown",
        notes=row["notes"],
    )


def get_db_path(db_path: str | Path | None = None) -> Path:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    if not path.exists():
        raise CCSError(f"CC Switch DB not found: {path}")
    return path


def list_providers(
    *,
    app_type: str = "codex",
    db_path: str | Path | None = None,
    include_incomplete: bool = False,
) -> list[CCSProvider]:
    path = get_db_path(db_path)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT id, app_type, name, is_current, settings_config, notes
            FROM providers
            WHERE (? = 'all' OR app_type = ?)
            ORDER BY is_current DESC, app_type, name
            """,
            (app_type, app_type),
        ).fetchall()
    finally:
        con.close()

    providers: list[CCSProvider] = []
    for row in rows:
        item = _provider_from_row(row)
        if item is None and include_incomplete:
            continue
        if item is not None:
            providers.append(item)
    return providers


def resolve_provider(
    name: str | None = None,
    *,
    app_type: str = "codex",
    db_path: str | Path | None = None,
    use_current: bool = False,
) -> CCSProvider:
    providers = list_providers(app_type=app_type, db_path=db_path)
    if not providers:
        raise CCSError(f"No usable CC Switch providers for app_type={app_type}")

    if use_current or not name or name.strip().lower() in {"current", "当前", "*"}:
        current = next((p for p in providers if p.is_current), None)
        if current:
            return current
        if not name:
            raise CCSError("No current provider in CC Switch; pass an explicit name.")

    needle = (name or "").strip().lower()
    # exact
    for p in providers:
        if p.name.lower() == needle:
            return p
    # contains
    contains = [p for p in providers if needle in p.name.lower()]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        # prefer current among matches
        for p in contains:
            if p.is_current:
                return p
        # A human shorthand such as "薄荷" should prefer "薄荷 level3" over
        # an unrelated concatenated name such as "薄荷codex". Keep true
        # multi-version matches ambiguous instead of choosing silently.
        token_prefix = [
            p
            for p in contains
            if re.match(rf"^{re.escape(needle)}(?:\s|[-_/])", p.name.lower())
        ]
        if len(token_prefix) == 1:
            return token_prefix[0]
        names = ", ".join(p.name for p in contains)
        raise CCSError(f"Multiple CC Switch providers match {name!r}: {names}")

    available = ", ".join(p.name for p in providers[:20])
    raise CCSError(f"CC Switch provider not found: {name!r}. Available: {available}")
