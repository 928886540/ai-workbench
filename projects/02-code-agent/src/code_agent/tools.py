"""Read-only tools for the first agent loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workbench_core.agent import AgentTool, ToolRegistry

from code_agent.workspace import Workspace, WorkspaceError

DEFAULT_SKIP_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    ".idea",
    ".vscode",
}

def list_dir(
    workspace: Workspace,
    relative_path: str = ".",
    max_entries: int = 100,
) -> dict[str, Any]:
    try:
        target = workspace.resolve(relative_path)
    except WorkspaceError as exc:
        return {"ok": False, "error": str(exc)}
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
    max_chars: int = 8000,
) -> dict[str, Any]:
    try:
        target = workspace.resolve(relative_path)
    except WorkspaceError as exc:
        return {"ok": False, "error": str(exc)}
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


def search_text(
    workspace: Workspace,
    query: str,
    relative_path: str = ".",
    max_matches: int = 20,
) -> dict[str, Any]:
    if not query.strip():
        return {"ok": False, "error": "query is empty"}

    try:
        root = workspace.resolve(relative_path)
    except WorkspaceError as exc:
        return {"ok": False, "error": str(exc)}

    matches: list[dict[str, Any]] = []
    files = [root] if root.is_file() else root.rglob("*")

    for path in files:
        if not path.is_file():
            continue
        if any(part in DEFAULT_SKIP_NAMES for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pyc", ".lock"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel = str(path.relative_to(workspace.root)).replace("\\", "/")
        for idx, line in enumerate(text.splitlines(), start=1):
            if query in line:
                matches.append(
                    {
                        "path": rel,
                        "line": idx,
                        "text": line.strip()[:240],
                    }
                )
                if len(matches) >= max_matches:
                    return {
                        "ok": True,
                        "query": query,
                        "matches": matches,
                        "truncated": True,
                    }

    return {
        "ok": True,
        "query": query,
        "matches": matches,
        "truncated": False,
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories under a workspace-relative path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "Relative directory path. Use '.' for workspace root.",
                        "default": ".",
                    },
                    "max_entries": {
                        "type": "integer",
                        "description": "Max entries to return.",
                        "default": 100,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "Relative file path to read.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters to return.",
                        "default": 8000,
                    },
                },
                "required": ["relative_path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "Search a literal text query across workspace files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Literal text to search.",
                    },
                    "relative_path": {
                        "type": "string",
                        "description": "Subdirectory or file to search in.",
                        "default": ".",
                    },
                    "max_matches": {
                        "type": "integer",
                        "description": "Max matches to return.",
                        "default": 20,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
]


class ToolRuntime(ToolRegistry):
    """Execute model-selected tools against one workspace."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        handlers = {
            "list_dir": lambda **kwargs: list_dir(self.workspace, **kwargs),
            "read_file": lambda **kwargs: read_file(self.workspace, **kwargs),
            "search_text": lambda **kwargs: search_text(self.workspace, **kwargs),
        }
        tools = []
        for schema in TOOL_SCHEMAS:
            function = schema["function"]
            tools.append(
                AgentTool(
                    name=function["name"],
                    description=function["description"],
                    parameters=function["parameters"],
                    handler=handlers[function["name"]],
                )
            )
        super().__init__(tools)
