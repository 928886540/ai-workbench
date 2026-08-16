"""MCP transports for the existing Leon image tool service."""

from __future__ import annotations

import argparse
import os
from typing import Annotated

from leon_agent.config import LeonSettings
from leon_agent.leon_client import LeonImageClient
from leon_agent.service import LeonToolService
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8240


def create_service(*, session_id: str, settings: LeonSettings | None = None) -> LeonToolService:
    config = settings or LeonSettings()
    client = LeonImageClient(
        backend_url=config.backend_url,
        public_base_url=config.active_public_image_base_url,
        plugin_dir=config.active_plugin_dir,
        timeout_seconds=config.http_timeout_seconds,
        bridge_timeout_seconds=config.bridge_timeout_seconds,
    )
    return LeonToolService(
        client,
        session_id=session_id,
        default_mode_ids=config.default_mode_ids,
        wait_for_image_completion=False,
    )


def create_mcp_server(service: LeonToolService) -> FastMCP:
    server = FastMCP(
        "Leon MCP Server",
        instructions=(
            "Use Leon's existing ComfyUI image tools. Pass image requests in source_text "
            "verbatim; do not rewrite or expand the user's prompt."
        ),
    )
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

    @server.tool(annotations=read_only)
    def list_image_modes() -> dict:
        """List installed Leon image modes with Chinese names, aliases, and exact IDs."""
        return service.list_image_modes()

    @server.tool(annotations=read_only)
    def check_image_environment() -> dict:
        """Check the Leon backend, required ComfyUI nodes, and LoRA availability."""
        return service.check_image_environment()

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        )
    )
    def generate_images(source_text: str, workflow_id: str | None = None) -> dict:
        """Submit exactly one image using the user's source text verbatim.

        Args:
            source_text: The user's current image request without rewriting or expansion.
            workflow_id: Optional exact mode ID returned by list_image_modes.
        """
        workflow_ids = [workflow_id] if workflow_id else None
        return service.generate_images(
            source_text=source_text,
            workflow_ids=workflow_ids,
            batch_count=1,
        )

    @server.tool(annotations=read_only)
    def get_image_tasks(
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get recent image task states for this MCP session."""
        return service.get_image_tasks(limit)

    @server.tool(annotations=read_only)
    def get_recent_images(
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get recent completed images created by this MCP session."""
        return service.get_recent_images(limit)

    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Expose Leon image tools through MCP")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--host", default=DEFAULT_HTTP_HOST, help="Streamable HTTP bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT, help="HTTP bind port")
    parser.add_argument(
        "--session-id",
        default=os.getenv("LEON_MCP_SESSION_ID", "default"),
        help="Stable Leon session scope used for task and gallery queries",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    service = create_service(session_id=args.session_id)
    server = create_mcp_server(service)
    server.settings.host = args.host
    server.settings.port = args.port
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
