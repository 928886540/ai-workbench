"""Read-only, workspace-bounded file search primitives."""

from workbench_core.files.service import FileSearchService
from workbench_core.files.workspace import Workspace, WorkspaceError

__all__ = ["FileSearchService", "Workspace", "WorkspaceError"]
