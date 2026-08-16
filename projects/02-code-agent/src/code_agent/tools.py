"""Read-only tools for the first agent loop."""

from __future__ import annotations

from typing import Any

from workbench_core.agent import AgentTool, ToolRegistry
from workbench_core.files import FileSearchService

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
    ".idea",
    ".vscode",
}

LEGACY_ROOT_ID = "workspace"
MAX_LEGACY_CHARS = 16_000


def _service(workspace: Workspace) -> FileSearchService:
    return FileSearchService({LEGACY_ROOT_ID: workspace.root})


def _legacy_list_dir(
    service: FileSearchService,
    relative_path: str = ".",
    max_entries: int = 100,
) -> dict[str, Any]:
    result = service.list_files(
        root_id=LEGACY_ROOT_ID,
        relative_path=relative_path,
        max_entries=max_entries,
    )
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "path": result["path"],
        "entries": result["entries"],
        "truncated": result["truncated"],
        "untrusted_content": True,
        "citation": result["citation"],
    }

def list_dir(
    workspace: Workspace,
    relative_path: str = ".",
    max_entries: int = 100,
) -> dict[str, Any]:
    return _legacy_list_dir(_service(workspace), relative_path, max_entries)


def _legacy_read_file(
    service: FileSearchService,
    relative_path: str,
    max_chars: int = 8000,
) -> dict[str, Any]:
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        return {
            "ok": False,
            "error_code": "invalid_argument",
            "error": "max_chars must be an integer.",
        }
    if max_chars < 1 or max_chars > MAX_LEGACY_CHARS:
        return {
            "ok": False,
            "error_code": "invalid_argument",
            "error": f"max_chars must be between 1 and {MAX_LEGACY_CHARS}.",
        }

    result = service.read_file(
        LEGACY_ROOT_ID,
        relative_path,
        start_line=1,
        max_lines=200,
    )
    if not result.get("ok"):
        return result
    content = result["content"][:max_chars]
    return {
        "ok": True,
        "path": result["path"],
        "content": content,
        "truncated": bool(result["truncated"] or len(result["content"]) > max_chars),
        "chars": len(content),
        "untrusted_content": True,
        "citation": result["citation"],
    }


def read_file(
    workspace: Workspace,
    relative_path: str,
    max_chars: int = 8000,
) -> dict[str, Any]:
    return _legacy_read_file(_service(workspace), relative_path, max_chars)


def _legacy_search_text(
    service: FileSearchService,
    query: str,
    relative_path: str = ".",
    max_matches: int = 20,
) -> dict[str, Any]:
    result = service.search(
        query,
        root_id=LEGACY_ROOT_ID,
        relative_path=relative_path,
        max_results=max_matches,
    )
    if not result.get("ok"):
        return result
    matches = [
        {
            "path": item["path"],
            "line": item["line"],
            "text": item["text"],
        }
        for item in result["matches"]
        if item["match_type"] == "content"
    ]
    return {
        "ok": True,
        "query": query,
        "matches": matches,
        "truncated": result["truncated"],
        "untrusted_content": True,
        "citation": [item["citation"] for item in result["matches"]],
    }


def search_text(
    workspace: Workspace,
    query: str,
    relative_path: str = ".",
    max_matches: int = 20,
) -> dict[str, Any]:
    return _legacy_search_text(_service(workspace), query, relative_path, max_matches)


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
                        "minimum": 1,
                        "maximum": 200,
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
                        "minimum": 1,
                        "maximum": 16000,
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
                        "minimum": 1,
                        "maximum": 50,
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
        self.service = _service(workspace)
        handlers = {
            "list_dir": lambda **kwargs: _legacy_list_dir(self.service, **kwargs),
            "read_file": lambda **kwargs: _legacy_read_file(self.service, **kwargs),
            "search_text": lambda **kwargs: _legacy_search_text(self.service, **kwargs),
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
