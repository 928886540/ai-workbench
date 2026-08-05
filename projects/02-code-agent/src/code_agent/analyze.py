"""Deterministic smoke entry for code-agent scaffolding.

Run:
  uv run python -m code_agent.analyze
"""

from __future__ import annotations

from pathlib import Path

from code_agent.agent import seed_analysis


def main() -> None:
    root = Path(__file__).resolve().parents[4]
    result = seed_analysis(str(root))
    print(f"workspace: {result.root}")
    print("top-level:")
    for name in result.top_level:
        print(f"  - {name}")
    if result.readme_excerpt:
        print("\nREADME excerpt:")
        print(result.readme_excerpt[:400])


if __name__ == "__main__":
    main()
