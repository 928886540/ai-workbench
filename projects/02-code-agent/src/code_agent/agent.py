"""Tool-calling agent loop for project analysis.

Core Agent learning flow:

    user question
      -> model may request tools
      -> runtime executes tools
      -> observations go back to model
      -> repeat until final answer
"""

from __future__ import annotations

from dataclasses import dataclass

from workbench_core.agent import AgentEvent, AgentResult, AgentRuntime, parse_tool_arguments
from workbench_core.llm import LLMClient

from code_agent.tools import ToolRuntime, list_dir, read_file
from code_agent.workspace import Workspace

SYSTEM_PROMPT = """
You are a code analysis agent for the ai-workbench learning lab.

Rules:
1. Use tools to inspect the real workspace before making claims.
2. Prefer list_dir -> read_file / search_text.
3. Stay inside the provided workspace. Never invent file contents.
4. When enough evidence is collected, stop calling tools and give the final answer.
5. Final answer must be Markdown with these sections:
   - 项目概览
   - 目录结构要点
   - 关键模块
   - 风险或改进建议
   - 证据路径
""".strip()


@dataclass
class AnalysisSeed:
    """Deterministic first pass without LLM."""

    root: str
    top_level: list[str]
    readme_excerpt: str | None


def seed_analysis(root: str) -> AnalysisSeed:
    ws = Workspace(root)
    listing = list_dir(ws, ".")
    names = [item["name"] for item in listing.get("entries", [])]

    readme_excerpt = None
    if "README.md" in names:
        readme = read_file(ws, "README.md", max_chars=1200)
        if readme.get("ok"):
            readme_excerpt = readme["content"]

    return AnalysisSeed(root=str(ws.root), top_level=names, readme_excerpt=readme_excerpt)


_parse_arguments = parse_tool_arguments


class CodeAgent:
    def __init__(
        self,
        workspace: Workspace,
        client: LLMClient | None = None,
        *,
        max_turns: int = 8,
        verbose: bool = True,
    ) -> None:
        self.workspace = workspace
        self.client = client or LLMClient()
        self.tools = ToolRuntime(workspace)
        self.max_turns = max_turns
        self.verbose = verbose
        self.runtime = AgentRuntime(
            client=self.client,
            tools=self.tools,
            system_prompt=SYSTEM_PROMPT,
            max_turns=max_turns,
            temperature=0.1,
            closing_prompt=(
                "Max tool turns reached. Stop calling tools. "
                "Write the final Markdown report now using evidence collected so far."
            ),
            on_event=self._on_event,
        )

    def _on_event(self, event: AgentEvent) -> None:
        if not self.verbose:
            return
        if event.kind == "turn_started":
            print(f"\n=== turn {event.turn}/{self.max_turns} ===")
        elif event.kind == "tool_started":
            print(f"tool: {event.tool_name}({event.arguments})")
        elif event.kind == "tool_finished":
            result = str(event.result)
            print(f"result: {result[:300]}")
        elif event.kind == "completed":
            print("final answer ready")

    def run(self, question: str) -> AgentResult:
        return self.runtime.run(
            f"Workspace root: {self.workspace.root}\n"
            f"Question: {question}\n"
            "Inspect the repository with tools, then produce the final report."
        )
