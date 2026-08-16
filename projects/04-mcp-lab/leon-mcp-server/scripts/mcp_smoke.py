"""Read-only MCP protocol smoke for interview demonstrations."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a read-only Leon MCP protocol smoke")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--url", default="http://127.0.0.1:8240/mcp")
    parser.add_argument(
        "--check-environment",
        action="store_true",
        help="Also call the read-only check_image_environment tool",
    )
    return parser


async def _show_tools(session: ClientSession, *, check_environment: bool) -> None:
    await session.initialize()
    result = await session.list_tools()
    print("tools/list:")
    for tool in result.tools:
        annotations = tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}
        print(f"- {tool.name}: annotations={annotations}")
    if check_environment:
        check = await session.call_tool("check_image_environment")
        payload: Any = check.structuredContent or check.content
        print("check_image_environment:")
        print(payload)


async def _run_stdio(*, check_environment: bool) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "leon_mcp_server.server", "--transport", "stdio"],
        env={**os.environ, "LEON_MCP_SESSION_ID": "interview-smoke"},
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await _show_tools(session, check_environment=check_environment)


async def _run_http(url: str, *, check_environment: bool) -> None:
    async with streamable_http_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await _show_tools(session, check_environment=check_environment)


async def run(args: argparse.Namespace) -> None:
    if args.transport == "stdio":
        await _run_stdio(check_environment=args.check_environment)
    else:
        await _run_http(args.url, check_environment=args.check_environment)


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
