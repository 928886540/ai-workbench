"""Code analysis agent entrypoint.

Examples:
  uv run python -m code_agent.analyze
  uv run python -m code_agent.analyze --question "这个仓库的学习主线是什么？"
  uv run python -m code_agent.analyze --seed
"""

from __future__ import annotations

import argparse
from pathlib import Path

from workbench_core.config import get_settings, reset_settings_cache
from workbench_core.llm import LLMClient
from workbench_core.logging import setup_logging

from code_agent.agent import CodeAgent, seed_analysis
from code_agent.workspace import Workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze a local project with a tool-using agent")
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[4]),
        help="Workspace root to analyze (default: ai-workbench repo root)",
    )
    parser.add_argument(
        "--question",
        default="请分析这个项目的目标、结构、关键模块，并给出改进建议。",
        help="Analysis question for the agent",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=8,
        help="Max model/tool loop turns",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Only run deterministic non-LLM seed analysis",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Hide per-turn tool logs",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    root = str(Path(args.root).resolve())

    if args.seed:
        result = seed_analysis(root)
        print(f"workspace: {result.root}")
        print("top-level:")
        for name in result.top_level:
            print(f"  - {name}")
        if result.readme_excerpt:
            print("\nREADME excerpt:")
            print(result.readme_excerpt[:400])
        return

    reset_settings_cache()
    settings = get_settings()
    setup_logging(settings.log_level)

    print(
        f"source={settings.llm_source} profile={settings.profile} "
        f"model={settings.active_model} root={root}"
    )

    agent = CodeAgent(
        workspace=Workspace(root),
        client=LLMClient(settings),
        max_turns=args.max_turns,
        verbose=not args.quiet,
    )
    result = agent.run(args.question)

    print("\n========== TOOL TRACE ==========")
    if not result.steps:
        print("(no tools used)")
    for idx, step in enumerate(result.steps, start=1):
        ok = step.result.get("ok")
        print(f"{idx}. {step.name} ok={ok} args={step.arguments}")

    print("\n========== FINAL REPORT ==========")
    print(result.answer)
    print(f"\n(turns={result.turns}, tools={len(result.steps)})")


if __name__ == "__main__":
    main()
