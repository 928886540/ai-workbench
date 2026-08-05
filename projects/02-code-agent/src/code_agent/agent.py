"""Agent loop placeholder.

Next implementation steps:
1. expose tools as function schemas
2. call model with tool definitions
3. execute returned tool calls
4. feed observations back until final answer
"""

from __future__ import annotations

from dataclasses import dataclass

from code_agent.tools import list_dir, read_file
from code_agent.workspace import Workspace


@dataclass
class AnalysisSeed:
    """Deterministic first pass before full LLM loop is implemented."""

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
