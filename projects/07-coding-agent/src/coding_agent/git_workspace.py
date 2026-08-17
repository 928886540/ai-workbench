"""Small Git adapter used only for tracked-file checks and final diff."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from workbench_core.files import Workspace

from coding_agent.execution import minimal_environment


class GitWorkspace:
    def __init__(self, workspace: Workspace, *, diff_chars: int = 16_000) -> None:
        self.workspace = workspace
        self._diff_chars = diff_chars
        top_level = self._run("rev-parse", "--show-toplevel")
        if top_level.returncode != 0:
            raise ValueError("workspace must be a Git repository")
        try:
            resolved_top_level = Path(top_level.stdout.decode("utf-8").strip()).resolve(strict=True)
        except (OSError, UnicodeError) as exc:
            raise ValueError("Git repository root could not be resolved") from exc
        if resolved_top_level != workspace.root:
            raise ValueError("workspace must be the Git repository root")

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.run(
            ("git", *arguments),
            cwd=self.workspace.root,
            env=minimal_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
            creationflags=creationflags,
        )

    def require_clean(self) -> None:
        status = self._run("status", "--porcelain", "--untracked-files=all")
        if status.returncode != 0 or status.stdout:
            raise ValueError("workspace must start from a clean Git worktree")

    def is_tracked(self, relative_path: str) -> bool:
        result = self._run("ls-files", "--error-unmatch", "--", relative_path)
        return result.returncode == 0

    def diff(self) -> dict[str, object]:
        names = self._run("diff", "--name-only", "-z", "--", ".")
        rendered = self._run("diff", "--no-ext-diff", "--", ".")
        if names.returncode != 0 or rendered.returncode != 0:
            return {
                "ok": False,
                "error_code": "git_failed",
                "error": "Git could not produce the workspace diff.",
            }
        changed_paths = tuple(
            item.decode("utf-8", errors="replace")
            for item in names.stdout.split(b"\x00")
            if item
        )
        text = rendered.stdout.decode("utf-8", errors="replace")
        truncated = len(text) > self._diff_chars
        if truncated:
            text = text[: self._diff_chars]
        return {
            "ok": True,
            "changed_paths": changed_paths,
            "changed_count": len(changed_paths),
            "diff": text,
            "empty": not text,
            "truncated": truncated,
        }
