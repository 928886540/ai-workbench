"""Read-only tools for the first agent loop.

Keep tools boring and explicit:
- clear input schema
- clear output payload
- no hidden side effects
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_agent.workspace import Workspace

DEFAULT_SKIP_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
}


def list_dir(workspace: Workspace, relative_path: str = ".", max_entries: int = 100) -> dict[str, Any]:
    target = workspace.resolve(relative_path)
    if not target.is_dir():
        return {"ok": False, "error": f"Not a directory: {relative_path}"}

    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name in DEFAULT_SKIP_NAMES:
            continue
        entries.append(
            {
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
            }
        )
        if len(entries) >= max_entries:
            break

    return {
        "ok": True,
        "path": str(Path(relative_path)),
        "entries": entries,
        "truncated": len(entries) >= max_entries,
    }


def read_file(
    workspace: Workspace,
    relative_path: str,
    *,
    max_chars: int = 8000,
) -> dict[str, Any]:
    target = workspace.resolve(relative_path)
    if not target.is_file():
        return {"ok": False, "error": f"Not a file: {relative_path}"}

    text = target.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > max_chars
    return {
        "ok": True,
        "path": relative_path,
        "content": text[:max_chars],
        "truncated": truncated,
        "chars": min(len(text), max_chars),
    }
