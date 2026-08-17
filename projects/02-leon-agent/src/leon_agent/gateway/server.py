"""Entry point for `leon-server` CLI command."""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Start Leon Agent HTTP Gateway")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1; expose it through Cloudflare Tunnel)",
    )
    parser.add_argument("--port", type=int, default=8233, help="Bind port (default: 8233)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev only)")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Uvicorn worker count (must remain 1; gateway state is process-local)",
    )
    args = parser.parse_args()
    if args.workers != 1:
        parser.error("leon-server only supports one worker; gateway state is process-local")

    import uvicorn

    uvicorn.run(
        "leon_agent.gateway.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=1,
        timeout_graceful_shutdown=10.0,
    )


if __name__ == "__main__":
    main()
