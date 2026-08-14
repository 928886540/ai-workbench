from pathlib import Path

import pytest
from code_agent.tools import ToolRuntime, list_dir, read_file, search_text
from code_agent.workspace import Workspace, WorkspaceError


def test_workspace_blocks_escape(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    with pytest.raises(WorkspaceError):
        ws.resolve("../outside.txt")


def test_list_and_read(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (tmp_path / "src").mkdir()

    ws = Workspace(tmp_path)
    listing = list_dir(ws, ".")
    assert listing["ok"] is True
    names = {item["name"] for item in listing["entries"]}
    assert names == {"README.md", "src"}

    content = read_file(ws, "README.md")
    assert content["ok"] is True
    assert "demo" in content["content"]


def test_search_text(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hello agent loop\n", encoding="utf-8")
    ws = Workspace(tmp_path)
    result = search_text(ws, "agent")
    assert result["ok"] is True
    assert result["matches"][0]["path"] == "a.py"


def test_tool_runtime_execute(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("workbench\n", encoding="utf-8")
    runtime = ToolRuntime(Workspace(tmp_path))
    result = runtime.execute("read_file", {"relative_path": "README.md"})
    assert result["ok"] is True
    assert "workbench" in result["content"]


def test_tool_runtime_unknown(tmp_path: Path) -> None:
    runtime = ToolRuntime(Workspace(tmp_path))
    result = runtime.execute("nope", {})
    assert result["ok"] is False
