"""Model-visible tools for the minimal Coding Agent vertical demo."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from workbench_core.agent import AgentTool, ToolRegistry
from workbench_core.files import FileSearchService, FileWriteService, Workspace, WorkspaceError

from coding_agent.execution import FixedTestRunner, TestAuthorization
from coding_agent.git_workspace import GitWorkspace

ROOT_ID = "workspace"
MAX_PLAN_STEPS = 4


def _error(error_code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "error": message}


def _audit_ok(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok") is True,
        **(
            {"error_code": result.get("error_code", "tool_failed")}
            if result.get("ok") is not True
            else {}
        ),
    }


def _audit_path_result(result: dict[str, Any]) -> dict[str, Any]:
    projected = _audit_ok(result)
    if result.get("ok") is True:
        projected["path"] = result.get("path")
        projected["truncated"] = result.get("truncated", False)
    return projected


def _audit_test_result(result: dict[str, Any]) -> dict[str, Any]:
    projected = _audit_ok(result)
    if result.get("ok") is True:
        for key in ("attempt", "passed", "exit_code", "timed_out", "truncated"):
            projected[key] = result.get(key)
    return projected


def _audit_write_result(result: dict[str, Any]) -> dict[str, Any]:
    projected = _audit_path_result(result)
    if result.get("ok") is True:
        projected["authorized"] = True
    return projected


class CodingToolRuntime(ToolRegistry):
    """One bounded coding task: plan, two writes/tests at most, then diff."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        test_command: Sequence[str],
        authorize_write=None,  # noqa: ANN001
        authorize_test: TestAuthorization | None = None,
        test_timeout_seconds: float = 60.0,
    ) -> None:
        self.workspace = workspace
        self.search = FileSearchService({ROOT_ID: workspace.root})
        self.writer = FileWriteService(
            {ROOT_ID: workspace.root},
            authorize=authorize_write,
            max_writes=2,
        )
        self.git = GitWorkspace(workspace)
        self.git.require_clean()
        self.tests = FixedTestRunner(
            workspace,
            test_command,
            authorize=authorize_test,
            max_runs=2,
            timeout_seconds=test_timeout_seconds,
        )
        self._plan: tuple[str, ...] = ()
        self._write_count = 0
        self._write_pending_test = False
        self._tests_passed = False

        super().__init__(
            [
                AgentTool(
                    name="list_files",
                    description="List text files under one workspace-relative directory.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "relative_path": {"type": "string", "default": "."},
                            "max_entries": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100,
                                "default": 50,
                            },
                        },
                        "additionalProperties": False,
                    },
                    handler=self._list_files,
                    audit_result=lambda result: {
                        **_audit_path_result(result),
                        "returned_entries": result.get("returned_entries", 0),
                    },
                ),
                AgentTool(
                    name="read_file",
                    description="Read a bounded section of an existing UTF-8 text file.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "relative_path": {"type": "string"},
                            "start_line": {"type": "integer", "minimum": 1, "default": 1},
                            "max_lines": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 200,
                                "default": 200,
                            },
                        },
                        "required": ["relative_path"],
                        "additionalProperties": False,
                    },
                    handler=self._read_file,
                    audit_arguments=lambda arguments: {
                        "relative_path": arguments.get("relative_path"),
                        "start_line": arguments.get("start_line", 1),
                        "max_lines": arguments.get("max_lines", 200),
                    },
                    audit_result=_audit_path_result,
                ),
                AgentTool(
                    name="search_code",
                    description="Search a literal string in workspace text files.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "minLength": 1},
                            "relative_path": {"type": "string", "default": "."},
                            "max_results": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 20,
                                "default": 20,
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                    handler=self._search_code,
                    audit_arguments=lambda arguments: {
                        "relative_path": arguments.get("relative_path", "."),
                        "max_results": arguments.get("max_results", 20),
                    },
                    audit_result=lambda result: {
                        **_audit_ok(result),
                        "result_count": len(result.get("matches", ())),
                        "truncated": result.get("truncated", False),
                    },
                ),
                AgentTool(
                    name="plan_create",
                    description="Create one concise 2-4 step plan before changing files.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "steps": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": MAX_PLAN_STEPS,
                                "items": {"type": "string", "minLength": 1, "maxLength": 120},
                            }
                        },
                        "required": ["steps"],
                        "additionalProperties": False,
                    },
                    handler=self._create_plan,
                    audit_arguments=lambda arguments: {
                        "step_count": len(arguments.get("steps", ()))
                    },
                    audit_result=lambda result: {
                        **_audit_ok(result),
                        "step_count": result.get("step_count", 0),
                    },
                    span_kind="planning",
                ),
                AgentTool(
                    name="write_file",
                    description=(
                        "Replace one existing tracked text file after an explicit server-side "
                        "authorization. Run tests before another write."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "relative_path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["relative_path", "content"],
                        "additionalProperties": False,
                    },
                    handler=self._write_file,
                    audit_arguments=lambda arguments: {
                        "relative_path": arguments.get("relative_path"),
                        "character_count": len(arguments.get("content", "")),
                    },
                    audit_result=_audit_write_result,
                ),
                AgentTool(
                    name="run_tests",
                    description=(
                        "Run the server-configured test command. No arguments are accepted."
                    ),
                    parameters={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=self._run_tests,
                    audit_arguments=lambda arguments: {},
                    audit_result=_audit_test_result,
                ),
                AgentTool(
                    name="show_diff",
                    description="Return the current tracked Git diff and changed paths.",
                    parameters={"type": "object", "properties": {}, "additionalProperties": False},
                    handler=self._show_diff,
                    audit_arguments=lambda arguments: {},
                    audit_result=lambda result: {
                        **_audit_ok(result),
                        "changed_count": result.get("changed_count", 0),
                        "truncated": result.get("truncated", False),
                    },
                ),
            ]
        )

    def _list_files(self, relative_path: str = ".", max_entries: int = 50) -> dict[str, Any]:
        return self.search.list_files(ROOT_ID, relative_path, max_entries)

    def _read_file(
        self,
        relative_path: str,
        start_line: int = 1,
        max_lines: int = 200,
    ) -> dict[str, Any]:
        return self.search.read_file(ROOT_ID, relative_path, start_line, max_lines)

    def _search_code(
        self,
        query: str,
        relative_path: str = ".",
        max_results: int = 20,
    ) -> dict[str, Any]:
        return self.search.search(query, ROOT_ID, relative_path, max_results)

    def _create_plan(self, steps: Any) -> dict[str, Any]:
        if self._plan:
            return _error("plan_exists", "A plan already exists for this task.")
        if not isinstance(steps, list) or not 2 <= len(steps) <= MAX_PLAN_STEPS:
            return _error("invalid_argument", "steps must contain two to four items.")
        normalized: list[str] = []
        for step in steps:
            if (
                not isinstance(step, str)
                or not step.strip()
                or len(step.strip()) > 120
                or any(ord(character) < 32 for character in step)
            ):
                return _error("invalid_argument", "Plan steps must be short single-line text.")
            normalized.append(step.strip())
        self._plan = tuple(normalized)
        return {"ok": True, "steps": self._plan, "step_count": len(self._plan)}

    def _write_file(self, relative_path: str, content: str) -> dict[str, Any]:
        if not self._plan:
            return _error("plan_required", "Create a plan before changing files.")
        if self._tests_passed:
            return _error("tests_already_passing", "No more writes are allowed after tests pass.")
        if self._write_pending_test:
            return _error("test_required", "Run tests before attempting another write.")
        if self._write_count >= 2:
            return _error("write_limit_reached", "Only one repair attempt is allowed.")
        try:
            normalized_path = self.workspace.relative(self.workspace.resolve(relative_path))
        except WorkspaceError as exc:
            return _error(exc.error_code, str(exc))
        if not self.git.is_tracked(normalized_path):
            return _error("untracked_file", "Only existing tracked files may be changed.")
        result = self.writer.write_file(ROOT_ID, normalized_path, content)
        if result.get("ok") is True:
            self._write_count += 1
            self._write_pending_test = True
        return result

    def _run_tests(self) -> dict[str, Any]:
        if not self._plan:
            return _error("plan_required", "Create a plan before running tests.")
        if not self._write_pending_test:
            return _error("write_required", "Change one file before running tests.")
        result = self.tests.run()
        if result.get("ok") is True:
            self._write_pending_test = False
            self._tests_passed = result.get("passed") is True
        return result

    def _show_diff(self) -> dict[str, Any]:
        if self._write_count == 0:
            return _error("write_required", "Change one file before requesting a diff.")
        return self.git.diff()
