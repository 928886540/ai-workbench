from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from workbench_core.files import Workspace


@pytest.fixture
def git_repo_factory(tmp_path: Path) -> Callable[[Mapping[str, str]], Workspace]:
    counter = 0

    def create(files: Mapping[str, str]) -> Workspace:
        nonlocal counter
        counter += 1
        root = tmp_path / f"repo-{counter}"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
        for relative_path, content in files.items():
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Coding Agent Test",
                "-c",
                "user.email=coding-agent@example.invalid",
                "commit",
                "-qm",
                "baseline",
            ],
            cwd=root,
            check=True,
        )
        return Workspace(root)

    return create
