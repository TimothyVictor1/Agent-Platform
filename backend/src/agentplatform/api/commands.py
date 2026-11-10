"""Enqueue API→runtime commands (models in ``contracts.commands``; none defined here).

Every command gets a fresh ``new_id()``. That id is what the endpoint returns as
``CommandAccepted.command_id`` and what the runtime copies into ``causation_id`` of the
events the command causes (SPEC §7.1), e.g. ``session.plan_updated`` for ``PUT /plan``.
``command.plan_session`` goes to ``NEW_SESSIONS_QUEUE``; everything else goes to the
session's own command queue.
"""

from __future__ import annotations

from typing import Any

import structlog

from agentplatform.api.metrics import Metrics
from agentplatform.bus.base import NEW_SESSIONS_QUEUE, EventBus, session_command_queue
from agentplatform.contracts.api import CommandAccepted
from agentplatform.contracts.commands import COMMAND_ADAPTER, Command, PlanSessionCommand
from agentplatform.contracts.common import Actor, new_id, utcnow

__all__ = ["CommandSender", "command_queue"]

log = structlog.get_logger(__name__)


def command_queue(command: Command) -> str:
    if isinstance(command, PlanSessionCommand):
        return NEW_SESSIONS_QUEUE
    return session_command_queue(command.session_id)


class CommandSender:
    def __init__(self, bus: EventBus, metrics: Metrics) -> None:
        self._bus = bus
        self._metrics = metrics

    def build(self, type_: str, session_id: str, **fields: Any) -> Command:
        """Validate a command of ``type_`` (e.g. ``"command.steer"``) issued by the user."""
        return COMMAND_ADAPTER.validate_python(
            {
                "type": type_,
                "id": new_id(),
                "session_id": session_id,
                "issued_at": utcnow(),
                "issued_by": Actor.user().model_dump(),
                **fields,
            }
        )

    async def send(self, command: Command) -> CommandAccepted:
        """Enqueue ``command``; the returned ``command_id`` is ``command.id``."""
        await self._bus.enqueue(command_queue(command), command)
        self._metrics.inc(f"commands.{command.type.removeprefix('command.')}")
        log.info(
            "command_enqueued",
            session_id=command.session_id,
            command_id=command.id,
            command_type=command.type,
        )
        return CommandAccepted(command_id=command.id)

    async def issue(self, type_: str, session_id: str, **fields: Any) -> CommandAccepted:
        return await self.send(self.build(type_, session_id, **fields))
