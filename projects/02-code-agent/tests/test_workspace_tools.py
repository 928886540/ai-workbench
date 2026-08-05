from pathlib import Path

from code_agent.tools import list_dir, read_file
from code_agent.workspace import Workspace, WorkspaceError
import pytest


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
