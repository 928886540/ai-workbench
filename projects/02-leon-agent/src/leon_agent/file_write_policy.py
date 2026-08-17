"""Per-turn authorization for Leon's bounded file write tools."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any
from weakref import WeakKeyDictionary

from workbench_core.files import FileWriteRequest, FileWriteService

_CURRENT_USER_MESSAGE: ContextVar[str | None] = ContextVar(
    "leon_file_write_user_message",
    default=None,
)
_COMMAND_PATTERN = re.compile(
    r"^!file[ \t]+(?P<operation>create|write)[ \t]+"
    r"(?P<root_id>[A-Za-z0-9_-]{1,32}):(?P<relative_path>[^\r\n]+)$"
)
_TURN_LOCKS: WeakKeyDictionary[FileWriteService, Any] = WeakKeyDictionary()
_TURN_LOCKS_GUARD = Lock()


@dataclass(frozen=True)
class FileWriteCommand:
    operation: str
    root_id: str
    relative_path: str


class FileWriteTurnBusyError(RuntimeError):
    """Raised when one service is accidentally shared by concurrent turns."""


def _turn_lock(service: FileWriteService) -> Any:
    with _TURN_LOCKS_GUARD:
        lock = _TURN_LOCKS.get(service)
        if lock is None:
            lock = Lock()
            _TURN_LOCKS[service] = lock
        return lock


def parse_file_write_command(user_message: Any) -> FileWriteCommand | None:
    """Parse an exact capability command from the first user-authored line."""

    if not isinstance(user_message, str) or not user_message:
        return None
    first_line = user_message.splitlines()[0]
    match = _COMMAND_PATTERN.fullmatch(first_line)
    if match is None:
        return None
    relative_path = match.group("relative_path").strip().replace("\\", "/")
    if not relative_path:
        return None
    return FileWriteCommand(
        operation=f"{match.group('operation')}_file",
        root_id=match.group("root_id"),
        relative_path=relative_path,
    )


def authorize_file_write(user_message: Any, request: FileWriteRequest) -> bool:
    """Authorize only an exact first-line command matching service metadata."""

    command = parse_file_write_command(user_message)
    if command is None:
        return False
    return (
        command.operation == request.operation
        and command.root_id == request.root_id
        and command.relative_path == request.relative_path.replace("\\", "/")
    )


def create_file_write_service(
    roots: Mapping[str, str | Path],
) -> FileWriteService | None:
    """Build the optional write service with context-local authorization."""

    if not roots:
        return None

    def authorize(request: FileWriteRequest) -> bool:
        return authorize_file_write(_CURRENT_USER_MESSAGE.get(), request)

    return FileWriteService(roots, authorize=authorize, max_writes=1)


@contextmanager
def file_write_turn(
    service: FileWriteService | None,
    user_message: str,
) -> Iterator[None]:
    """Bind authorization to one turn and reset its single-write budget."""

    if service is None:
        yield
        return
    turn_lock = _turn_lock(service)
    if not turn_lock.acquire(blocking=False):
        raise FileWriteTurnBusyError("file write service already has an active turn")
    try:
        service.reset_write_budget()
        token = _CURRENT_USER_MESSAGE.set(user_message)
        try:
            yield
        finally:
            _CURRENT_USER_MESSAGE.reset(token)
    finally:
        turn_lock.release()


__all__ = [
    "FileWriteCommand",
    "FileWriteTurnBusyError",
    "authorize_file_write",
    "create_file_write_service",
    "file_write_turn",
    "parse_file_write_command",
]
