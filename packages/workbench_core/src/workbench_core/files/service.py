"""Bounded, read-only file listing, reading, and literal search."""

from __future__ import annotations

import codecs
import hashlib
import os
import re
import secrets
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from workbench_core.files.workspace import Workspace, WorkspaceError

MAX_ROOTS = 8
MAX_LIST_ENTRIES = 200
MAX_LIST_SCAN_ENTRIES = 2_000
MAX_QUERY_CHARS = 256
MAX_SEARCH_RESULTS = 50
MAX_FILE_BYTES = 1024 * 1024
MAX_READ_CHARS = 16_000
MAX_READ_LINES = 200
MAX_SEARCH_FILES = 2_000
MAX_SEARCH_BYTES = 20 * 1024 * 1024
MAX_SEARCH_DIRECTORIES = 1_000
MAX_SEARCH_ENTRIES = 10_000
MAX_SNIPPET_CHARS = 320

ROOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ROOT_BINDING_KEY = secrets.token_bytes(32)

BLOCKED_NAMES = frozenset(
    {
        ".aws",
        ".azure",
        ".git",
        ".gnupg",
        ".hg",
        ".idea",
        ".mypy_cache",
        ".netrc",
        ".npmrc",
        ".pytest_cache",
        ".pypirc",
        ".ruff_cache",
        ".ssh",
        ".svn",
        ".venv",
        ".vscode",
        "__pycache__",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "node_modules",
        "secrets.json",
        "venv",
    }
)
BLOCKED_SUFFIXES = frozenset(
    {
        ".db",
        ".db-journal",
        ".db-shm",
        ".db-wal",
        ".journal",
        ".key",
        ".p12",
        ".pem",
        ".pfx",
        ".shm",
        ".sqlite",
        ".sqlite3",
        ".sqlite-journal",
        ".sqlite-shm",
        ".sqlite-wal",
        ".ppk",
        ".wal",
    }
)
BLOCKED_NAME_ENDINGS = ("-journal", "-shm", "-wal")
BLOCKED_BINARY_SIGNATURES = (
    b"\x7fELF",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"7z\xbc\xaf\x27\x1c",
    b"GIF87a",
    b"GIF89a",
    b"MZ",
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"Rar!\x1a\x07",
    b"SQLite format 3\x00",
    b"%PDF-",
    b"\x1f\x8b",
)
PRIVATE_KEY_MARKERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
)
SUPPORTED_TEXT_SUFFIXES = frozenset(
    {
        "",
        ".bat",
        ".c",
        ".cfg",
        ".cmd",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".csv",
        ".ejs",
        ".go",
        ".h",
        ".hpp",
        ".htm",
        ".html",
        ".ini",
        ".j2",
        ".java",
        ".jinja",
        ".js",
        ".json",
        ".jsonl",
        ".jsx",
        ".kt",
        ".log",
        ".markdown",
        ".md",
        ".php",
        ".prompt",
        ".ps1",
        ".py",
        ".pyi",
        ".rb",
        ".rs",
        ".rst",
        ".scss",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
)


def _error(error_code: str, message: str, **metadata: Any) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "error": message, **metadata}


def _success(**data: Any) -> dict[str, Any]:
    return {"ok": True, "untrusted_content": True, **data}


def _validate_int(
    value: Any,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> dict[str, Any] | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return _error("invalid_argument", f"{name} must be an integer.")
    if value < minimum or value > maximum:
        return _error(
            "invalid_argument",
            f"{name} must be between {minimum} and {maximum}.",
        )
    return None


def _normalized_name(name: str) -> str:
    return name.rstrip(" .").casefold()


def _root_binding_fingerprint(path: Path) -> str:
    """Return a process-local opaque token for one canonical root path."""
    normalized = os.path.normcase(os.fspath(path))
    return hashlib.blake2s(
        os.fsencode(normalized),
        key=_ROOT_BINDING_KEY,
        digest_size=32,
    ).hexdigest()


def _is_blocked_path(relative_path: str) -> bool:
    parts = Path(relative_path).parts
    for part in parts:
        normalized = _normalized_name(part)
        if normalized.startswith("."):
            return True
        if normalized in BLOCKED_NAMES:
            return True
        if normalized.startswith(".env"):
            return True
    normalized_name = _normalized_name(Path(relative_path).name)
    if normalized_name.endswith(BLOCKED_NAME_ENDINGS):
        return True
    return Path(relative_path).suffix.casefold() in BLOCKED_SUFFIXES


def _is_supported_file(path: Path) -> bool:
    return path.suffix.casefold() in SUPPORTED_TEXT_SUFFIXES


class _ReadError(Exception):
    def __init__(self, error_code: str, message: str, *, bytes_read: int = 0) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.bytes_read = bytes_read


def _read_text(path: Path, *, byte_limit: int = MAX_FILE_BYTES) -> tuple[str, int]:
    if not _is_supported_file(path):
        raise _ReadError("unsupported_file_type", "The file type is not supported.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise _ReadError("io_error", "The file could not be inspected.") from exc
    if size > MAX_FILE_BYTES:
        raise _ReadError("file_too_large", "The file exceeds the 1 MiB limit.")
    if size > byte_limit:
        raise _ReadError("scan_budget", "The remaining search byte budget is too small.")

    try:
        with path.open("rb") as stream:
            payload = stream.read(min(MAX_FILE_BYTES, byte_limit) + 1)
    except OSError as exc:
        raise _ReadError("io_error", "The file could not be read.") from exc
    if len(payload) > MAX_FILE_BYTES:
        raise _ReadError(
            "file_too_large",
            "The file exceeds the 1 MiB limit.",
            bytes_read=len(payload),
        )
    if len(payload) > byte_limit:
        raise _ReadError(
            "scan_budget",
            "The remaining search byte budget is too small.",
            bytes_read=len(payload),
        )
    if payload.startswith(BLOCKED_BINARY_SIGNATURES):
        raise _ReadError(
            "binary_file",
            "Binary files cannot be read.",
            bytes_read=len(payload),
        )

    try:
        if payload.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            text = payload.decode("utf-16", errors="strict")
        else:
            if b"\x00" in payload:
                raise _ReadError(
                    "binary_file",
                    "Binary files cannot be read.",
                    bytes_read=len(payload),
                )
            text = payload.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise _ReadError(
            "unsupported_encoding",
            "The file must use UTF-8 or BOM-marked UTF-16 encoding.",
            bytes_read=len(payload),
        ) from exc
    if any(
        (ord(character) < 32 and character not in "\t\n\r\f")
        or 0x7F <= ord(character) <= 0x9F
        for character in text
    ):
        raise _ReadError(
            "binary_file",
            "Binary files cannot be read.",
            bytes_read=len(payload),
        )
    if any(marker in text.upper() for marker in PRIVATE_KEY_MARKERS):
        raise _ReadError(
            "sensitive_content",
            "Private key material cannot be read.",
            bytes_read=len(payload),
        )
    return text, len(payload)


@dataclass
class _WalkState:
    directories: int = 0
    entries: int = 0
    truncation_reason: str | None = None


class FileSearchService:
    """Search up to eight named roots without exposing their absolute paths."""

    def __init__(self, roots: Mapping[str, str | Path]) -> None:
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

        self._roots: dict[str, Workspace] = {}
        self._root_bindings: dict[str, str] = {}
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
            candidate = Path(root).expanduser()
            if not candidate.is_absolute():
                raise WorkspaceError(
                    "File roots must use absolute paths.",
                    error_code="invalid_argument",
                )
            workspace = Workspace(candidate)
            self._roots[root_id] = workspace
            self._root_bindings[root_id] = _root_binding_fingerprint(workspace.root)

    @property
    def root_ids(self) -> list[str]:
        return list(self._roots)

    @property
    def root_bindings(self) -> dict[str, str]:
        """Return opaque composition tokens without exposing absolute roots."""
        return dict(self._root_bindings)

    def _workspace(self, root_id: Any) -> tuple[str, Workspace] | dict[str, Any]:
        if root_id is None:
            if not self._roots:
                return _error("unknown_root", "No file roots are configured.")
            if len(self._roots) != 1:
                return _error(
                    "invalid_argument",
                    "root_id is required when multiple file roots are configured.",
                    root_ids=self.root_ids,
                )
            selected_id = next(iter(self._roots))
            return selected_id, self._roots[selected_id]
        if not isinstance(root_id, str):
            return _error("invalid_argument", "root_id must be a string.")
        workspace = self._roots.get(root_id)
        if workspace is None:
            return _error(
                "unknown_root",
                "The requested file root is not configured.",
                root_ids=self.root_ids,
            )
        return root_id, workspace

    def _resolve(
        self,
        workspace: Workspace,
        relative_path: Any,
    ) -> Path | dict[str, Any]:
        if not isinstance(relative_path, (str, Path)):
            return _error("invalid_argument", "relative_path must be a string.")
        if _is_blocked_path(str(relative_path).replace("\\", "/")):
            return _error("blocked_path", "The requested path is blocked by policy.")
        try:
            return workspace.resolve(relative_path)
        except WorkspaceError as exc:
            return _error(exc.error_code, str(exc))

    def list_files(
        self,
        root_id: str | None = None,
        relative_path: str = ".",
        max_entries: int = 100,
    ) -> dict[str, Any]:
        invalid = _validate_int(
            max_entries,
            name="max_entries",
            minimum=1,
            maximum=MAX_LIST_ENTRIES,
        )
        if invalid:
            return invalid
        if root_id is None:
            if str(relative_path).replace("\\", "/") not in {"", "."}:
                return _error(
                    "invalid_argument",
                    "relative_path requires an explicit root_id.",
                )
            all_root_ids = self.root_ids
            root_ids = all_root_ids[:max_entries]
            entries = [
                {"name": item, "path": item, "type": "root"}
                for item in root_ids
            ]
            return _success(
                root_ids=root_ids,
                path=".",
                citation=[f"{item}:" for item in root_ids],
                entries=entries,
                returned_entries=len(entries),
                skipped_entries=0,
                truncated=len(root_ids) < len(all_root_ids),
                max_entries=max_entries,
            )
        selected = self._workspace(root_id)
        if isinstance(selected, dict):
            return selected
        selected_id, workspace = selected
        target = self._resolve(workspace, relative_path)
        if isinstance(target, dict):
            return target
        if not target.exists():
            return _error("not_found", "The requested path does not exist.")
        if not target.is_dir():
            return _error("not_directory", "The requested path is not a directory.")

        entries: list[dict[str, str]] = []
        skipped_entries = 0
        truncated = False
        try:
            children = list(islice(target.iterdir(), MAX_LIST_SCAN_ENTRIES + 1))
        except OSError:
            return _error("io_error", "The directory could not be listed.")
        if len(children) > MAX_LIST_SCAN_ENTRIES:
            children = children[:MAX_LIST_SCAN_ENTRIES]
            truncated = True
        children.sort(key=lambda item: item.name.casefold())
        for child in children:
            child_relative = workspace.relative(child)
            if _is_blocked_path(child_relative):
                skipped_entries += 1
                continue
            safe_child = self._resolve(workspace, child_relative)
            if isinstance(safe_child, dict):
                skipped_entries += 1
                continue
            if not safe_child.exists():
                skipped_entries += 1
                continue
            if safe_child.is_file() and not _is_supported_file(safe_child):
                skipped_entries += 1
                continue
            if not safe_child.is_dir() and not safe_child.is_file():
                skipped_entries += 1
                continue
            if len(entries) >= max_entries:
                truncated = True
                break
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if safe_child.is_dir() else "file",
                }
            )

        normalized_path = workspace.relative(target)
        return _success(
            root_id=selected_id,
            path=normalized_path,
            citation=f"{selected_id}:{normalized_path}",
            entries=entries,
            returned_entries=len(entries),
            skipped_entries=skipped_entries,
            truncated=truncated,
            max_entries=max_entries,
        )

    def read_file(
        self,
        root_id: str,
        relative_path: str,
        start_line: int = 1,
        max_lines: int = 200,
    ) -> dict[str, Any]:
        invalid = _validate_int(
            start_line,
            name="start_line",
            minimum=1,
            maximum=2_147_483_647,
        )
        if invalid:
            return invalid
        invalid = _validate_int(
            max_lines,
            name="max_lines",
            minimum=1,
            maximum=MAX_READ_LINES,
        )
        if invalid:
            return invalid
        selected = self._workspace(root_id)
        if isinstance(selected, dict):
            return selected
        selected_id, workspace = selected
        target = self._resolve(workspace, relative_path)
        if isinstance(target, dict):
            return target
        if not target.exists():
            return _error("not_found", "The requested file does not exist.")
        if not target.is_file():
            return _error("not_file", "The requested path is not a file.")

        try:
            text, byte_count = _read_text(target)
        except _ReadError as exc:
            return _error(exc.error_code, str(exc))

        lines = text.splitlines()
        total_lines = len(lines)
        start_index = start_line - 1
        if start_index >= total_lines and total_lines > 0:
            return _error("invalid_argument", "start_line exceeds the file length.")
        selected_lines = lines[start_index : start_index + max_lines]
        content = "\n".join(selected_lines)
        char_truncated = len(content) > MAX_READ_CHARS
        if char_truncated:
            content = content[:MAX_READ_CHARS]

        returned_lines = content.count("\n") + (1 if content else 0)
        end_line = start_line + returned_lines - 1 if returned_lines else 0
        line_truncated = start_index + len(selected_lines) < total_lines
        truncated = char_truncated or line_truncated
        normalized_path = workspace.relative(target)
        citation = (
            f"{selected_id}:{normalized_path}:{start_line}-{end_line}"
            if returned_lines
            else f"{selected_id}:{normalized_path}"
        )
        return _success(
            root_id=selected_id,
            path=normalized_path,
            citation=citation,
            content=content,
            start_line=start_line,
            end_line=end_line,
            returned_lines=returned_lines,
            total_lines=total_lines,
            chars=len(content),
            bytes=byte_count,
            truncated=truncated,
            truncation={
                "line_limit": line_truncated,
                "response_char_limit": char_truncated,
            },
        )

    def _walk_files(
        self,
        workspace: Workspace,
        relative_path: str,
        state: _WalkState,
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> Iterator[tuple[Path, str]]:
        if cancel_check is not None:
            cancel_check()
        target = self._resolve(workspace, relative_path)
        if isinstance(target, dict) or not target.exists():
            return
        if target.is_file():
            yield target, workspace.relative(target)
            return
        if not target.is_dir():
            return

        stack = [workspace.relative(target)]
        while stack:
            if cancel_check is not None:
                cancel_check()
            if state.directories >= MAX_SEARCH_DIRECTORIES:
                state.truncation_reason = "directory_budget"
                return
            directory_relative = stack.pop()
            directory = self._resolve(workspace, directory_relative)
            if isinstance(directory, dict) or not directory.is_dir():
                continue
            state.directories += 1
            remaining_entries = MAX_SEARCH_ENTRIES - state.entries
            if remaining_entries <= 0:
                state.truncation_reason = "entry_budget"
                return
            try:
                children: list[Path] = []
                for child in islice(directory.iterdir(), remaining_entries + 1):
                    if cancel_check is not None:
                        cancel_check()
                    children.append(child)
            except OSError:
                continue
            if len(children) > remaining_entries:
                children = children[:remaining_entries]
                state.truncation_reason = "entry_budget"
            children.sort(key=lambda item: item.name.casefold())
            child_directories: list[str] = []
            for child in children:
                if cancel_check is not None:
                    cancel_check()
                state.entries += 1
                child_relative = workspace.relative(child)
                if _is_blocked_path(child_relative):
                    continue
                safe_child = self._resolve(workspace, child_relative)
                if isinstance(safe_child, dict) or not safe_child.exists():
                    continue
                if safe_child.is_dir():
                    child_directories.append(child_relative)
                elif safe_child.is_file():
                    yield safe_child, child_relative
            if state.truncation_reason is not None:
                return
            stack.extend(reversed(child_directories))

    def search(
        self,
        query: str,
        root_id: str | None = None,
        relative_path: str = ".",
        max_results: int = 20,
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if cancel_check is not None:
            cancel_check()
        if not isinstance(query, str) or not query.strip():
            return _error("invalid_argument", "query must be a non-empty string.")
        if len(query) > MAX_QUERY_CHARS:
            return _error(
                "invalid_argument",
                f"query cannot exceed {MAX_QUERY_CHARS} characters.",
            )
        invalid = _validate_int(
            max_results,
            name="max_results",
            minimum=1,
            maximum=MAX_SEARCH_RESULTS,
        )
        if invalid:
            return invalid
        if not isinstance(relative_path, (str, Path)):
            return _error("invalid_argument", "relative_path must be a string.")

        if root_id is None and str(relative_path).replace("\\", "/") not in {"", "."}:
            return _error(
                "invalid_argument",
                "relative_path requires an explicit root_id.",
            )

        if root_id is None:
            if not self._roots:
                return _error("unknown_root", "No file roots are configured.")
            selected_roots = list(self._roots.items())
        else:
            selected = self._workspace(root_id)
            if isinstance(selected, dict):
                return selected
            selected_roots = [selected]

        query_folded = query.casefold()
        matches: list[dict[str, Any]] = []
        considered_files = 0
        scanned_files = 0
        scanned_bytes = 0
        skipped_files = 0
        truncated = False
        truncation_reason: str | None = None
        walk_state = _WalkState()

        for selected_id, workspace in selected_roots:
            if cancel_check is not None:
                cancel_check()
            initial = self._resolve(workspace, relative_path)
            if isinstance(initial, dict):
                return initial
            if not initial.exists():
                return _error("not_found", "The requested search path does not exist.")
            if not initial.is_file() and not initial.is_dir():
                return _error("invalid_path", "The requested search path is not searchable.")

            for path, normalized_path in self._walk_files(
                workspace,
                str(relative_path),
                walk_state,
                cancel_check=cancel_check,
            ):
                if cancel_check is not None:
                    cancel_check()
                if considered_files >= MAX_SEARCH_FILES:
                    truncated = True
                    truncation_reason = "file_budget"
                    break
                considered_files += 1

                if not _is_supported_file(path):
                    skipped_files += 1
                    continue

                if query_folded in Path(normalized_path).name.casefold():
                    matches.append(
                        {
                            "root_id": selected_id,
                            "path": normalized_path,
                            "match_type": "filename",
                            "line": None,
                            "text": Path(normalized_path).name,
                            "citation": f"{selected_id}:{normalized_path}",
                            "untrusted_content": True,
                        }
                    )
                    if len(matches) >= max_results:
                        truncated = True
                        truncation_reason = "result_limit"
                        break

                try:
                    size = path.stat().st_size
                except OSError:
                    skipped_files += 1
                    continue
                if size > MAX_FILE_BYTES:
                    skipped_files += 1
                    continue
                remaining_bytes = MAX_SEARCH_BYTES - scanned_bytes
                if size > remaining_bytes:
                    truncated = True
                    truncation_reason = "byte_budget"
                    break
                try:
                    text, byte_count = _read_text(path, byte_limit=remaining_bytes)
                except _ReadError as exc:
                    scanned_bytes += exc.bytes_read
                    if exc.error_code == "scan_budget":
                        truncated = True
                        truncation_reason = "byte_budget"
                        break
                    skipped_files += 1
                    continue
                if cancel_check is not None:
                    cancel_check()
                scanned_files += 1
                scanned_bytes += byte_count

                for line_number, line in enumerate(text.splitlines(), start=1):
                    if cancel_check is not None:
                        cancel_check()
                    if query_folded not in line.casefold():
                        continue
                    snippet = line.strip()[:MAX_SNIPPET_CHARS]
                    matches.append(
                        {
                            "root_id": selected_id,
                            "path": normalized_path,
                            "match_type": "content",
                            "line": line_number,
                            "text": snippet,
                            "citation": f"{selected_id}:{normalized_path}:{line_number}",
                            "untrusted_content": True,
                        }
                    )
                    if len(matches) >= max_results:
                        truncated = True
                        truncation_reason = "result_limit"
                        break
                if truncated:
                    break
            if walk_state.truncation_reason is not None and not truncated:
                truncated = True
                truncation_reason = walk_state.truncation_reason
            if truncated:
                break

        if cancel_check is not None:
            cancel_check()
        citations = list(dict.fromkeys(match["citation"] for match in matches))
        return _success(
            query=query,
            root_ids=[item[0] for item in selected_roots],
            relative_path=str(relative_path).replace("\\", "/") or ".",
            citation=citations,
            matches=matches,
            returned_results=len(matches),
            considered_files=considered_files,
            scanned_files=scanned_files,
            scanned_bytes=scanned_bytes,
            skipped_files=skipped_files,
            truncated=truncated,
            truncation_reason=truncation_reason,
            limits={
                "max_results": max_results,
                "max_files": MAX_SEARCH_FILES,
                "max_bytes": MAX_SEARCH_BYTES,
                "max_directories": MAX_SEARCH_DIRECTORIES,
                "max_entries": MAX_SEARCH_ENTRIES,
            },
        )
