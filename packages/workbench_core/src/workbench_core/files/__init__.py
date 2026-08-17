"""Workspace-bounded file search and explicitly authorized write primitives."""

from workbench_core.files.service import FileSearchService
from workbench_core.files.workspace import Workspace, WorkspaceError
from workbench_core.files.write_service import (
    MAX_WRITE_BYTES,
    FileWriteRequest,
    FileWriteService,
)

__all__ = [
    "FileSearchService",
    "FileWriteRequest",
    "FileWriteService",
    "MAX_WRITE_BYTES",
    "Workspace",
    "WorkspaceError",
]
