"""CLI helpers for CC Switch provider discovery."""

from __future__ import annotations

import argparse

from workbench_core.ccs import CCSError, list_providers, resolve_provider


def main() -> None:
    parser = argparse.ArgumentParser(description="List/resolve CC Switch LLM providers")
    parser.add_argument("command", choices=["list", "show"], help="list or show provider")
    parser.add_argument("name", nargs="?", help="provider name for show / fuzzy match")
    parser.add_argument("--app", default="codex", help="codex|claude|all (default codex)")
    args = parser.parse_args()

    try:
        if args.command == "list":
            providers = list_providers(app_type=args.app)
            if not providers:
                print("No providers found.")
                return
            for p in providers:
                print(p.summary())
            return

        provider = resolve_provider(args.name, app_type=args.app, use_current=not args.name)
        print(provider.summary())
        print(f"id={provider.id}")
        print(f"base_url={provider.base_url}")
        print(f"model={provider.model}")
        print(f"api_key={provider.api_key[:4]}...{provider.api_key[-4:]}")
    except CCSError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
