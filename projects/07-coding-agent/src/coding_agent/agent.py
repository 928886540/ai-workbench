"""A minimal vertical Coding Agent built on the shared AgentRuntime."""

from __future__ import annotations

from collections.abc import Sequence

from workbench_core.agent import AgentResult, AgentRuntime, TraceContext, TraceSink
from workbench_core.files import Workspace
from workbench_core.llm import LLMClient

from coding_agent.execution import TestAuthorization
from coding_agent.tools import CodingToolRuntime

SYSTEM_PROMPT = """
You are a small coding-agent lab, not a general autonomous developer.

For each task:
1. Inspect real files with list_files, read_file, or search_code.
2. Call plan_create once with 2-4 concise steps before any write.
3. Change only an existing tracked text file with write_file.
4. Call run_tests after every write. If the first run fails, use its output to repair once.
5. Stop after the second test run, call show_diff, and summarize changed files and test status.

Never invent file contents, command arguments, test results, or Git output. Do not create files,
commit, push, reset, change branches, or ask for an arbitrary shell command.
""".strip()


class CodingAgent:
    """One interview-sized coding task with at most one repair attempt."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        test_command: Sequence[str],
        client: LLMClient | None = None,
        authorize_write=None,  # noqa: ANN001
        authorize_test: TestAuthorization | None = None,
        test_timeout_seconds: float = 60.0,
        max_turns: int = 12,
    ) -> None:
        self.workspace = workspace
        self.client = client or LLMClient()
        self.tools = CodingToolRuntime(
            workspace,
            test_command=test_command,
            authorize_write=authorize_write,
            authorize_test=authorize_test,
            test_timeout_seconds=test_timeout_seconds,
        )
        self.runtime = AgentRuntime(
            client=self.client,
            tools=self.tools,
            system_prompt=SYSTEM_PROMPT,
            max_turns=max_turns,
            temperature=0.1,
            closing_prompt=(
                "Stop changing files. Report the observed test result and current Git diff."
            ),
        )

    def run(
        self,
        task: str,
        *,
        trace_context: TraceContext | None = None,
        trace_sink: TraceSink | None = None,
    ) -> AgentResult:
        return self.runtime.run(
            task,
            trace_context=trace_context,
            trace_sink=trace_sink,
        )
