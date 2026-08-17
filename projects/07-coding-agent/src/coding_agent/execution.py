"""Fixed-command execution for the Coding Agent demo."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from workbench_core.files import Workspace

DEFAULT_OUTPUT_CHARS = 8_000
DEFAULT_TIMEOUT_SECONDS = 60.0
_ENV_ALLOWLIST = (
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
)


@dataclass(frozen=True, slots=True)
class TestRunRequest:
    """Safe metadata passed to the server-side authorization hook."""

    profile_id: str
    attempt: int


TestAuthorization = Callable[[TestRunRequest], bool]


def minimal_environment() -> dict[str, str]:
    """Keep runtime variables while excluding provider keys and arbitrary secrets."""

    environment = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class FixedTestRunner:
    """Run one composition-root command without accepting model-supplied argv."""

    def __init__(
        self,
        workspace: Workspace,
        command: Sequence[str],
        *,
        authorize: TestAuthorization | None = None,
        max_runs: int = 2,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        output_chars: int = DEFAULT_OUTPUT_CHARS,
    ) -> None:
        argv = tuple(command)
        invalid_argument = any(
            not isinstance(item, str) or not item or "\x00" in item for item in argv
        )
        if not argv or invalid_argument:
            raise ValueError("command must contain non-empty string arguments")
        if isinstance(max_runs, bool) or not isinstance(max_runs, int) or not 1 <= max_runs <= 2:
            raise ValueError("max_runs must be one or two")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 300
        ):
            raise ValueError("timeout_seconds must be between zero and 300")
        if (
            isinstance(output_chars, bool)
            or not isinstance(output_chars, int)
            or output_chars < 1
        ):
            raise ValueError("output_chars must be a positive integer")
        if authorize is not None and not callable(authorize):
            raise ValueError("authorize must be callable")

        self.workspace = workspace
        self._command = argv
        self._authorize = authorize
        self._max_runs = max_runs
        self._timeout_seconds = float(timeout_seconds)
        self._output_chars = output_chars
        self._runs_used = 0

    def _authorized(self, request: TestRunRequest) -> bool:
        if self._authorize is None:
            return False
        try:
            return self._authorize(request) is True
        except Exception:
            return False

    def _bounded_output(self, output: str) -> tuple[str, bool]:
        root_variants = {str(self.workspace.root), self.workspace.root.as_posix()}
        for root in sorted(root_variants, key=len, reverse=True):
            output = output.replace(root, "<workspace>")
        if len(output) <= self._output_chars:
            return output, False
        return output[: self._output_chars], True

    def run(self) -> dict[str, object]:
        attempt = self._runs_used + 1
        if self._runs_used >= self._max_runs:
            return {
                "ok": False,
                "error_code": "test_limit_reached",
                "error": "The test command may run at most twice per task.",
            }
        request = TestRunRequest(profile_id="tests", attempt=attempt)
        if not self._authorized(request):
            return {
                "ok": False,
                "error_code": "authorization_required",
                "error": "The test command was not authorized.",
            }

        self._runs_used += 1
        started = time.monotonic()
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                self._command,
                cwd=self.workspace.root,
                env=minimal_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self._timeout_seconds,
                check=False,
                shell=False,
                creationflags=creationflags,
            )
            output, truncated = self._bounded_output(_decode_output(completed.stdout))
            return {
                "ok": True,
                "profile_id": "tests",
                "attempt": attempt,
                "passed": completed.returncode == 0,
                "exit_code": completed.returncode,
                "timed_out": False,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "output": output,
                "truncated": truncated,
            }
        except subprocess.TimeoutExpired as exc:
            output, truncated = self._bounded_output(_decode_output(exc.stdout))
            return {
                "ok": True,
                "profile_id": "tests",
                "attempt": attempt,
                "passed": False,
                "exit_code": None,
                "timed_out": True,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "output": output,
                "truncated": truncated,
            }
        except OSError:
            return {
                "ok": False,
                "error_code": "process_start_failed",
                "error": "The fixed test command could not be started.",
            }
