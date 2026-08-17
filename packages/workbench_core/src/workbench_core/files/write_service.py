"""Explicitly authorized, workspace-bounded text file writes."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from workbench_core.files.service import (
    BLOCKED_BINARY_SIGNATURES,
    MAX_ROOTS,
    PRIVATE_KEY_MARKERS,
    ROOT_ID_PATTERN,
    _is_blocked_path,
    _is_supported_file,
    _read_text,
    _ReadError,
    _root_binding_fingerprint,
)
from workbench_core.files.workspace import (
    Workspace,
    WorkspaceError,
    _unsafe_file_attributes,
)

MAX_WRITE_BYTES = 64 * 1024
MAX_RELATIVE_PATH_CHARS = 500

WriteOperation = Literal["create_file", "write_file"]
AuthorizationHook = Callable[["FileWriteRequest"], bool]
_FileIdentity = tuple[int, int]


@dataclass(frozen=True)
class FileWriteRequest:
    """Metadata exposed to the server-side authorization hook."""

    operation: WriteOperation
    root_id: str
    relative_path: str
    byte_count: int


def _error(error_code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "error": message}


def _entry_identity(metadata: os.stat_result) -> _FileIdentity:
    return metadata.st_dev, metadata.st_ino


def _contains_unsafe_control_characters(text: str) -> bool:
    return any(
        (ord(character) < 32 and character not in "\t\n\r\f") or 0x7F <= ord(character) <= 0x9F
        for character in text
    )


def _replace_existing_file(source: Path, target: Path) -> None:
    """Atomically replace an existing target without creating a missing one."""
    if os.name != "nt":
        os.replace(source, target)
        return

    # ReplaceFileW requires the destination to exist, unlike MoveFileEx/os.replace.
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    replace_file.restype = wintypes.BOOL
    replace_through = 0x00000001  # REPLACEFILE_WRITE_THROUGH
    if not replace_file(
        str(target),
        str(source),
        None,
        replace_through,
        None,
        None,
    ):
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "The existing file could not be replaced.")


class FileWriteService:
    """Create or replace text files only after out-of-band authorization.

    One instance represents one write budget. Composition roots should create a
    fresh instance for each Agent turn instead of sharing it across sessions.
    """

    def __init__(
        self,
        roots: Mapping[str, str | Path],
        *,
        authorize: AuthorizationHook | None = None,
        max_writes: int = 1,
    ) -> None:
        if not isinstance(roots, Mapping):
            raise WorkspaceError(
                "File roots must be a mapping.",
                error_code="invalid_argument",
            )
        if len(roots) > MAX_ROOTS:
            raise WorkspaceError(
                f"At most {MAX_ROOTS} file roots may be configured.",
                error_code="invalid_argument",
            )
        if authorize is not None and not callable(authorize):
            raise WorkspaceError(
                "authorize must be callable.",
                error_code="invalid_argument",
            )
        if isinstance(max_writes, bool) or not isinstance(max_writes, int):
            raise WorkspaceError(
                "max_writes must be a positive integer.",
                error_code="invalid_argument",
            )
        if max_writes < 1:
            raise WorkspaceError(
                "max_writes must be a positive integer.",
                error_code="invalid_argument",
            )

        self._roots: dict[str, Workspace] = {}
        self._root_bindings: dict[str, str] = {}
        self._root_identities: dict[str, _FileIdentity] = {}
        seen_casefolded: set[str] = set()
        for root_id, root in roots.items():
            if not isinstance(root_id, str) or not ROOT_ID_PATTERN.fullmatch(root_id):
                raise WorkspaceError(
                    "Root ids must match [A-Za-z0-9_-]{1,32}.",
                    error_code="invalid_argument",
                )
            normalized_id = root_id.casefold()
            if normalized_id in seen_casefolded:
                raise WorkspaceError(
                    "Root ids must be unique ignoring case.",
                    error_code="invalid_argument",
                )
            seen_casefolded.add(normalized_id)
            try:
                candidate = Path(root).expanduser()
            except TypeError as exc:
                raise WorkspaceError(
                    "File roots must be path-like values.",
                    error_code="invalid_argument",
                ) from exc
            if not candidate.is_absolute():
                raise WorkspaceError(
                    "File roots must use absolute paths.",
                    error_code="invalid_argument",
                )
            workspace = Workspace(candidate)
            try:
                root_metadata = workspace.root.lstat()
            except OSError as exc:
                raise WorkspaceError(
                    "File root could not be inspected.",
                    error_code="io_error",
                ) from exc
            self._roots[root_id] = workspace
            self._root_bindings[root_id] = _root_binding_fingerprint(workspace.root)
            self._root_identities[root_id] = _entry_identity(root_metadata)

        self._authorize = authorize
        self._max_writes = max_writes
        self._writes_used = 0
        self._write_lock = threading.Lock()

    @property
    def root_ids(self) -> list[str]:
        return list(self._roots)

    @property
    def root_bindings(self) -> dict[str, str]:
        """Return opaque composition tokens without exposing absolute roots."""
        return dict(self._root_bindings)

    @property
    def authorization_configured(self) -> bool:
        return self._authorize is not None

    def reset_write_budget(self) -> None:
        """Reset this instance for a new turn; never expose this as a tool."""
        with self._write_lock:
            self._writes_used = 0

    def create_file(
        self,
        root_id: str,
        relative_path: str,
        content: str,
    ) -> dict[str, Any]:
        return self._write("create_file", root_id, relative_path, content)

    def write_file(
        self,
        root_id: str,
        relative_path: str,
        content: str,
    ) -> dict[str, Any]:
        return self._write("write_file", root_id, relative_path, content)

    def _write(
        self,
        operation: WriteOperation,
        root_id: Any,
        relative_path: Any,
        content: Any,
    ) -> dict[str, Any]:
        selected = self._workspace(root_id)
        if isinstance(selected, dict):
            return selected
        workspace = selected

        resolved = self._resolve_target(workspace, relative_path)
        if isinstance(resolved, dict):
            return resolved
        target, normalized_path = resolved

        encoded = self._encode_content(content)
        if isinstance(encoded, dict):
            return encoded

        request = FileWriteRequest(
            operation=operation,
            root_id=root_id,
            relative_path=normalized_path,
            byte_count=len(encoded),
        )
        if not self._is_authorized(request):
            return _error(
                "authorization_required",
                "This file write was not authorized.",
            )

        with self._write_lock:
            refreshed = self._resolve_target(workspace, normalized_path)
            if isinstance(refreshed, dict):
                return refreshed
            target, refreshed_path = refreshed
            if refreshed_path != normalized_path:
                return _error(
                    "path_changed",
                    "The requested path changed during validation.",
                )

            parent_state = self._validate_parent(root_id, workspace, target.parent)
            if isinstance(parent_state, dict):
                return parent_state

            target_state = self._validate_target(operation, target)
            if isinstance(target_state, dict):
                return target_state

            if self._writes_used >= self._max_writes:
                return _error(
                    "write_limit_reached",
                    "The write limit for this turn has been reached.",
                )
            self._writes_used += 1

            committed = self._commit(
                operation=operation,
                root_id=root_id,
                workspace=workspace,
                target=target,
                normalized_path=normalized_path,
                payload=encoded,
                parent_identity=parent_state,
                target_identity=target_state,
            )
            if isinstance(committed, dict):
                return committed

        result: dict[str, Any] = {
            "ok": True,
            "root_id": root_id,
            "path": normalized_path,
            "citation": f"{root_id}:{normalized_path}",
            "bytes": len(encoded),
        }
        if operation == "create_file":
            result["created"] = True
        else:
            result["overwritten"] = True
        return result

    def _workspace(self, root_id: Any) -> Workspace | dict[str, Any]:
        if not isinstance(root_id, str):
            return _error("invalid_argument", "root_id must be a string.")
        workspace = self._roots.get(root_id)
        if workspace is None:
            return _error("unknown_root", "The requested file root is not configured.")
        return workspace

    def _resolve_target(
        self,
        workspace: Workspace,
        relative_path: Any,
    ) -> tuple[Path, str] | dict[str, Any]:
        if not isinstance(relative_path, str):
            return _error("invalid_argument", "relative_path must be a string.")
        if len(relative_path) > MAX_RELATIVE_PATH_CHARS:
            return _error("invalid_argument", "relative_path is too long.")
        portable_path = relative_path.replace("\\", "/")
        if any(part == ".." for part in portable_path.split("/")):
            return _error(
                "path_outside_root",
                "Parent path components are not allowed for file writes.",
            )
        if _is_blocked_path(portable_path):
            return _error("blocked_path", "The requested path is blocked by policy.")
        try:
            target = workspace.resolve(relative_path)
            normalized_path = workspace.relative(target)
        except WorkspaceError as exc:
            return _error(exc.error_code, str(exc))
        if normalized_path == ".":
            return _error("invalid_path", "A file path is required.")
        if _is_blocked_path(normalized_path):
            return _error("blocked_path", "The requested path is blocked by policy.")
        if not _is_supported_file(target):
            return _error(
                "unsupported_file_type",
                "The file type is not supported.",
            )
        return target, normalized_path

    def _encode_content(self, content: Any) -> bytes | dict[str, Any]:
        if not isinstance(content, str):
            return _error("invalid_argument", "content must be a string.")
        # UTF-8 uses at least one byte per code point; this bounds expensive
        # scans and upper/encode copies before checking the exact byte count.
        if len(content) > MAX_WRITE_BYTES:
            return _error(
                "file_too_large",
                "content exceeds the 64 KiB write limit.",
            )
        if _contains_unsafe_control_characters(content):
            return _error("binary_file", "Binary content cannot be written.")
        if any(marker in content.upper() for marker in PRIVATE_KEY_MARKERS):
            return _error(
                "sensitive_content",
                "Private key material cannot be written.",
            )
        try:
            encoded = content.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return _error(
                "unsupported_encoding",
                "content must be valid Unicode text.",
            )
        if encoded.startswith(BLOCKED_BINARY_SIGNATURES):
            return _error("binary_file", "Binary content cannot be written.")
        if len(encoded) > MAX_WRITE_BYTES:
            return _error(
                "file_too_large",
                "content exceeds the 64 KiB write limit.",
            )
        return encoded

    def _is_authorized(self, request: FileWriteRequest) -> bool:
        if self._authorize is None:
            return False
        try:
            return self._authorize(request) is True
        except Exception:
            return False

    def _validate_parent(
        self,
        root_id: str,
        workspace: Workspace,
        parent: Path,
    ) -> _FileIdentity | dict[str, Any]:
        try:
            root_metadata = workspace.root.lstat()
        except OSError:
            return _error("io_error", "The file root could not be inspected.")
        if _entry_identity(root_metadata) != self._root_identities[root_id]:
            return _error("path_changed", "The file root changed during validation.")
        try:
            root_is_unsafe = _unsafe_file_attributes(workspace.root)
        except WorkspaceError as exc:
            return _error(exc.error_code, str(exc))
        if root_is_unsafe:
            return _error("blocked_path", "The file root is blocked by policy.")

        try:
            normalized_parent = workspace.relative(parent)
            checked_parent = workspace.resolve(normalized_parent)
        except WorkspaceError as exc:
            return _error(exc.error_code, str(exc))
        if checked_parent != parent:
            return _error("path_changed", "The parent directory changed.")

        try:
            metadata = parent.lstat()
        except FileNotFoundError:
            return _error(
                "parent_not_found",
                "The parent directory does not exist.",
            )
        except OSError:
            return _error("io_error", "The parent directory could not be inspected.")
        try:
            parent_is_unsafe = _unsafe_file_attributes(parent)
        except WorkspaceError as exc:
            return _error(exc.error_code, str(exc))
        if parent_is_unsafe:
            return _error("blocked_path", "The parent directory is blocked by policy.")
        if not stat.S_ISDIR(metadata.st_mode):
            return _error("not_directory", "The parent path is not a directory.")
        try:
            resolved_parent = parent.resolve(strict=True)
            resolved_parent.relative_to(workspace.root)
        except ValueError:
            return _error("path_outside_root", "The parent directory is outside the root.")
        except (OSError, RuntimeError):
            return _error("io_error", "The parent directory could not be resolved.")
        if resolved_parent != parent:
            return _error("path_changed", "The parent directory changed.")
        try:
            final_metadata = parent.lstat()
        except FileNotFoundError:
            return _error("path_changed", "The parent directory changed.")
        except OSError:
            return _error("io_error", "The parent directory could not be inspected.")
        if _entry_identity(final_metadata) != _entry_identity(metadata):
            return _error("path_changed", "The parent directory changed.")
        return _entry_identity(final_metadata)

    def _validate_target(
        self,
        operation: WriteOperation,
        target: Path,
    ) -> _FileIdentity | None | dict[str, Any]:
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            if operation == "create_file":
                return None
            return _error("not_found", "The requested file does not exist.")
        except OSError:
            return _error("io_error", "The requested file could not be inspected.")

        try:
            target_is_unsafe = _unsafe_file_attributes(target)
        except WorkspaceError as exc:
            return _error(exc.error_code, str(exc))
        if target_is_unsafe:
            return _error("blocked_path", "The requested file is blocked by policy.")
        if operation == "create_file":
            return _error("already_exists", "The requested file already exists.")
        if not stat.S_ISREG(metadata.st_mode):
            return _error("not_file", "The requested path is not a regular file.")
        identity = _entry_identity(metadata)
        try:
            _read_text(target)
        except _ReadError as exc:
            return _error(exc.error_code, str(exc))
        try:
            refreshed = target.lstat()
        except OSError:
            return _error("path_changed", "The requested file changed.")
        if _entry_identity(refreshed) != identity or not stat.S_ISREG(refreshed.st_mode):
            return _error("path_changed", "The requested file changed.")
        return identity

    def _commit(
        self,
        *,
        operation: WriteOperation,
        root_id: str,
        workspace: Workspace,
        target: Path,
        normalized_path: str,
        payload: bytes,
        parent_identity: _FileIdentity,
        target_identity: _FileIdentity | None,
    ) -> None | dict[str, Any]:
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".leon-write-",
                suffix=".tmp",
                dir=target.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

            refreshed = self._resolve_target(workspace, normalized_path)
            if isinstance(refreshed, dict):
                return refreshed
            refreshed_target, refreshed_path = refreshed
            if refreshed_target != target or refreshed_path != normalized_path:
                return _error("path_changed", "The requested path changed.")
            refreshed_parent = self._validate_parent(root_id, workspace, target.parent)
            if isinstance(refreshed_parent, dict):
                return refreshed_parent
            if refreshed_parent != parent_identity:
                return _error("path_changed", "The parent directory changed.")

            refreshed_target_state = self._validate_target(operation, target)
            if isinstance(refreshed_target_state, dict):
                return refreshed_target_state
            if refreshed_target_state != target_identity:
                return _error("path_changed", "The requested file changed.")

            if operation == "create_file":
                try:
                    os.link(temporary_path, target)
                except FileExistsError:
                    return _error(
                        "already_exists",
                        "The requested file already exists.",
                    )
            else:
                _replace_existing_file(temporary_path, target)
                temporary_path = None
        except OSError:
            return _error("io_error", "The file could not be written atomically.")
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return None


__all__ = ["FileWriteRequest", "FileWriteService", "MAX_WRITE_BYTES"]
