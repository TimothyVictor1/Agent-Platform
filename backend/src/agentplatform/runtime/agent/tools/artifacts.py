"""Artifact tools registered against the T06 ToolContext, with injected metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentplatform.artifacts.service import ArtifactService
from agentplatform.contracts.model_io import ToolSpec
from agentplatform.errors import InvalidRequestError
from agentplatform.runtime.agent.tools.registry import ToolRegistry, ToolResult

if TYPE_CHECKING:
    from agentplatform.runtime.agent.loop import ToolContext


def register_artifact_tools(registry: ToolRegistry, service: ArtifactService) -> None:
    async def publish(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.artifacts is None or ctx.board is None:
            raise InvalidRequestError("artifact storage and board must be configured")
        artifact = await service.publish(
            store=ctx.artifacts,
            board=ctx.board,
            sandbox=ctx.sandbox,
            session_id=ctx.session_id,
            actor=ctx.actor,
            task_key=ctx.task.key,
            step=ctx.step,
            path=args["sandbox_path"],
            name=args.get("name"),
            description=args.get("description"),
        )
        return ToolResult(text=artifact.model_dump_json())

    async def fetch(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.artifacts is None:
            raise InvalidRequestError("artifact storage must be configured")
        path = await service.fetch(
            args["artifact_id"],
            store=ctx.artifacts,
            sandbox=ctx.sandbox,
            session_id=ctx.session_id,
            actor=ctx.actor,
            agent_id=ctx.agent.id,
            directory=args.get("sandbox_dir", "/home/agent/Shared"),
        )
        return ToolResult(text=path)

    for name, description, properties, required, handler in (
        (
            "publish_artifact",
            "Publish a file under /home/agent to the session.",
            {
                "sandbox_path": {"type": "string"},
                "name": {"type": "string", "maxLength": 255},
                "description": {"type": "string", "maxLength": 500},
            },
            ["sandbox_path"],
            publish,
        ),
        (
            "fetch_artifact",
            "Fetch a session artifact into /home/agent/Shared.",
            {"artifact_id": {"type": "string"}, "sandbox_dir": {"type": "string"}},
            ["artifact_id"],
            fetch,
        ),
    ):
        registry.register(
            "artifacts",
            ToolSpec(
                name=name,
                description=description,
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            ),
            handler,
            kind="coordination",
        )
