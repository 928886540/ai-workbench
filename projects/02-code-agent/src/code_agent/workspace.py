"""Workspace-bounded path helpers.

Why:
- tool-using agents become dangerous if paths are unconstrained
- every file tool should resolve + validate against a root
"""

from __future__ import annotations

from pathlib import Path


class WorkspaceError(ValueError):
    pass


class Workspace:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.exists() or not self.root.is_dir():
            raise WorkspaceError(f"Workspace root is not a directory: {self.root}")

    def resolve(self, relative_path: str | Path = ".") -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(
                f"Path escapes workspace root: {relative_path} -> {candidate}"
            ) from exc
        return candidate
