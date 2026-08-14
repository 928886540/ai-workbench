"""Model-visible tools backed by the Leon image client."""

from __future__ import annotations

import time
from uuid import uuid4

from workbench_core.agent import AgentTool, ToolRegistry

from leon_agent.leon_client import LeonImageClient


def create_leon_tools(
    client: LeonImageClient,
    *,
    session_id: str,
    default_mode_ids: list[str],
) -> ToolRegistry:
    chat_id = f"leon-agent:{session_id}"

    def generate_images(
        source_text: str,
        workflow_ids: list[str] | None = None,
        batch_count: int = 1,
        character_context: str = "",
        random_workflow: bool = False,
    ) -> dict:
        modes = workflow_ids or default_mode_ids
        return client.generate_images(
            source_text=source_text,
            workflow_ids=modes,
            batch_count=batch_count,
            chat_id=chat_id,
            message_id=f"cli-{int(time.time() * 1000)}-{uuid4().hex[:6]}",
            character_context=character_context,
            random_workflow=random_workflow,
        )

    return ToolRegistry(
        [
            AgentTool(
                name="list_image_modes",
                description="List image modes available in the installed Leon image plugin.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=client.list_modes,
            ),
            AgentTool(
                name="check_image_environment",
                description=(
                    "Check Leon backend connectivity and required ComfyUI nodes and LoRAs. "
                    "This does not submit an image task."
                ),
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=client.check_environment,
            ),
            AgentTool(
                name="generate_images",
                description=(
                    "Submit images to the existing Leon/ComfyUI pipeline. Call only when the user "
                    "explicitly asks to create or generate images. "
                    "batch_count is per selected mode."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_text": {
                            "type": "string",
                            "description": "The complete scene or image request from the user.",
                        },
                        "workflow_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional mode ids from list_image_modes.",
                        },
                        "batch_count": {
                            "type": "integer",
                            "enum": [1, 2, 3, 5, 10],
                            "default": 1,
                        },
                        "character_context": {
                            "type": "string",
                            "description": (
                                "Optional stable character identity and appearance context."
                            ),
                        },
                        "random_workflow": {"type": "boolean", "default": False},
                    },
                    "required": ["source_text"],
                    "additionalProperties": False,
                },
                handler=generate_images,
            ),
            AgentTool(
                name="get_image_tasks",
                description="Get recent image task states created by this Leon Agent session.",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
                    },
                    "additionalProperties": False,
                },
                handler=lambda limit=20: client.get_image_tasks(chat_id=chat_id, limit=limit),
            ),
            AgentTool(
                name="get_recent_images",
                description="Get recent completed images created by this Leon Agent session.",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
                    },
                    "additionalProperties": False,
                },
                handler=lambda limit=20: client.get_recent_images(chat_id=chat_id, limit=limit),
            ),
        ]
    )
