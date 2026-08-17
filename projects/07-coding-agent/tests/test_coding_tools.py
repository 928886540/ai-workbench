from __future__ import annotations

import sys
from pathlib import Path

import pytest
from coding_agent import CodingToolRuntime, FixedTestRunner, Workspace


def _allow(_request) -> bool:  # noqa: ANN001
    return True


def _content_test(expected: str) -> tuple[str, ...]:
    script = (
        "from pathlib import Path; "
        "text=Path('app.py').read_text(encoding='utf-8'); "
        f"raise SystemExit(0 if {expected!r} in text else 1)"
    )
    return sys.executable, "-c", script


def test_side_effects_are_denied_without_server_authorization(git_repo_factory) -> None:  # noqa: ANN001
    workspace = git_repo_factory({"app.py": "VALUE = 'before'\n"})
    tools = CodingToolRuntime(workspace, test_command=_content_test("after"))

    assert tools.execute("plan_create", {"steps": ["edit", "test"]})["ok"] is True
    write = tools.execute(
        "write_file",
        {"relative_path": "app.py", "content": "VALUE = 'after'\n"},
    )
    command = tools.tests.run()

    assert write["error_code"] == "authorization_required"
    assert command["error_code"] == "authorization_required"
    assert (workspace.root / "app.py").read_text(encoding="utf-8") == "VALUE = 'before'\n"


def test_fixed_runner_hides_secrets_workspace_path_and_long_output(
    git_repo_factory,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = git_repo_factory({"app.py": "pass\n"})
    monkeypatch.setenv("CODING_AGENT_PRIVATE_KEY", "must-not-leak")
    script = (
        "import os; from pathlib import Path; "
        "print(os.getenv('CODING_AGENT_PRIVATE_KEY')); print(Path.cwd()); print('x' * 200)"
    )
    runner = FixedTestRunner(
        workspace,
        (sys.executable, "-c", script),
        authorize=_allow,
        output_chars=80,
    )

    result = runner.run()

    assert result["passed"] is True
    assert result["truncated"] is True
    assert "must-not-leak" not in result["output"]
    assert str(workspace.root) not in result["output"]
    assert "<workspace>" in result["output"]


def test_one_failed_test_allows_one_repair_then_stops(git_repo_factory) -> None:  # noqa: ANN001
    workspace = git_repo_factory({"app.py": "VALUE = 'before'\n"})
    tools = CodingToolRuntime(
        workspace,
        test_command=_content_test("VALUE = 'fixed'"),
        authorize_write=_allow,
        authorize_test=_allow,
    )

    assert tools.execute("plan_create", {"steps": ["edit", "test"]})["ok"] is True
    untracked = tools.execute(
        "write_file",
        {"relative_path": "new.py", "content": "pass\n"},
    )
    assert untracked["error_code"] == "untracked_file"

    first_write = tools.execute(
        "write_file",
        {"relative_path": "app.py", "content": "VALUE = 'wrong'\n"},
    )
    premature_repair = tools.execute(
        "write_file",
        {"relative_path": "app.py", "content": "VALUE = 'fixed'\n"},
    )
    first_test = tools.execute("run_tests", {})
    second_write = tools.execute(
        "write_file",
        {"relative_path": "app.py", "content": "VALUE = 'fixed'\n"},
    )
    second_test = tools.execute("run_tests", {})
    third_write = tools.execute(
        "write_file",
        {"relative_path": "app.py", "content": "VALUE = 'extra'\n"},
    )
    diff = tools.execute("show_diff", {})

    assert first_write["ok"] is True
    assert premature_repair["error_code"] == "test_required"
    assert first_test["passed"] is False
    assert second_write["ok"] is True
    assert second_test["passed"] is True
    assert third_write["error_code"] == "tests_already_passing"
    assert diff["changed_paths"] == ("app.py",)
    assert "VALUE = 'fixed'" in diff["diff"]


def test_runner_times_out_and_enforces_two_run_limit(git_repo_factory) -> None:  # noqa: ANN001
    workspace = git_repo_factory({"app.py": "pass\n"})
    slow = FixedTestRunner(
        workspace,
        (sys.executable, "-c", "import time; time.sleep(1)"),
        authorize=_allow,
        timeout_seconds=0.05,
    )

    assert slow.run()["timed_out"] is True
    assert slow.run()["timed_out"] is True
    assert slow.run()["error_code"] == "test_limit_reached"


def test_runtime_requires_clean_repository(git_repo_factory) -> None:  # noqa: ANN001
    workspace = git_repo_factory({"app.py": "pass\n"})
    (workspace.root / "app.py").write_text("changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="clean Git worktree"):
        CodingToolRuntime(
            workspace,
            test_command=(sys.executable, "-c", "raise SystemExit(0)"),
        )


def test_workspace_escape_is_rejected_before_write(git_repo_factory, tmp_path: Path) -> None:  # noqa: ANN001
    workspace: Workspace = git_repo_factory({"app.py": "pass\n"})
    outside = tmp_path / "outside.py"
    outside.write_text("unchanged\n", encoding="utf-8")
    tools = CodingToolRuntime(
        workspace,
        test_command=(sys.executable, "-c", "raise SystemExit(0)"),
        authorize_write=_allow,
        authorize_test=_allow,
    )
    tools.execute("plan_create", {"steps": ["edit", "test"]})

    result = tools.execute(
        "write_file",
        {"relative_path": "../outside.py", "content": "changed\n"},
    )

    assert result["error_code"] == "path_outside_root"
    assert outside.read_text(encoding="utf-8") == "unchanged\n"
