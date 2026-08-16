"""Filesystem path containment used by read-only Agent tools."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PureWindowsPath


class WorkspaceError(ValueError):
    """A public-safe path validation error."""

    def __init__(self, message: str, *, error_code: str = "invalid_path") -> None:
        super().__init__(message)
        self.error_code = error_code


def _unsafe_file_attributes(path: Path) -> bool:
    """Reject links, Windows reparse points, and hidden/system entries."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise WorkspaceError(
            "The requested path could not be inspected.",
            error_code="io_error",
        ) from exc

    if stat.S_ISLNK(metadata.st_mode):
        return True

    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    hidden = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
    system = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)
    return bool(attributes & (reparse | hidden | system))


def _relative_parts(relative_path: str | os.PathLike[str]) -> tuple[str, ...]:
    try:
        raw = os.fspath(relative_path)
    except TypeError as exc:
        raise WorkspaceError(
            "Path must be a string or path-like value.",
            error_code="invalid_argument",
        ) from exc
    if not isinstance(raw, str):
        raise WorkspaceError(
            "Path must be text.",
            error_code="invalid_argument",
        )
    if "\x00" in raw:
        raise WorkspaceError("Path contains an invalid character.")

    windows_path = PureWindowsPath(raw)
    native_path = Path(raw.replace("\\", "/"))
    if (
        windows_path.drive
        or windows_path.root
        or windows_path.anchor
        or native_path.drive
        or native_path.root
        or native_path.anchor
    ):
        raise WorkspaceError("Absolute and drive-relative paths are not allowed.")

    normalized: list[str] = []
    for part in native_path.parts:
        if part in {"", "."}:
            continue
        if ":" in part:
            raise WorkspaceError("Alternate data streams are not allowed.")
        if part == "..":
            if not normalized:
                raise WorkspaceError(
                    "Path escapes the configured root.",
                    error_code="path_outside_root",
                )
            normalized.pop()
            continue
        if part.endswith((" ", ".")):
            raise WorkspaceError("Path components cannot end with spaces or dots.")
        is_reserved = getattr(os.path, "isreserved", None)
        reserved = (
            bool(is_reserved(part))
            if callable(is_reserved)
            else PureWindowsPath(part).is_reserved()
        )
        if reserved:
            raise WorkspaceError("Reserved path names are not allowed.")
        normalized.append(part)
    return tuple(normalized)


class Workspace:
    """Resolve untrusted relative paths inside one trusted directory root."""

    def __init__(self, root: str | Path) -> None:
        try:
            source = Path(root).expanduser()
        except TypeError as exc:
            raise WorkspaceError(
                "Workspace root must be a path-like value.",
                error_code="invalid_argument",
            ) from exc

        if not source.exists():
            raise WorkspaceError("Workspace root does not exist.", error_code="not_found")
        if _unsafe_file_attributes(source):
            raise WorkspaceError(
                "Workspace root is not an allowed filesystem entry.",
                error_code="blocked_path",
            )
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceError(
                "Workspace root could not be resolved.",
                error_code="io_error",
            ) from exc
        if not resolved.is_dir():
            raise WorkspaceError(
                "Workspace root is not a directory.",
                error_code="not_directory",
            )
        self.root = resolved

    def resolve(self, relative_path: str | Path = ".") -> Path:
        parts = _relative_parts(relative_path)
        current = self.root
        for part in parts:
            current = current / part
            if _unsafe_file_attributes(current):
                raise WorkspaceError(
                    "The requested path is blocked by the filesystem policy.",
                    error_code="blocked_path",
                )

        try:
            candidate = current.resolve(strict=False)
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(
                "Path escapes the configured root.",
                error_code="path_outside_root",
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError(
                "The requested path could not be resolved.",
                error_code="invalid_path",
            ) from exc
        return candidate

    def relative(self, path: Path) -> str:
        """Return a normalized path without exposing the absolute root."""
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(
                "Path escapes the configured root.",
                error_code="path_outside_root",
            ) from exc
        value = relative.as_posix()
        return value or "."
