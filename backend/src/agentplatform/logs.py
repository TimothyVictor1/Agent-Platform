"""structlog setup shared by the API and runtime processes (SPEC §19)."""

from __future__ import annotations

import logging
import sys

import structlog

__all__ = ["configure_logging"]


def configure_logging(level: str = "INFO", *, json: bool = True) -> None:
    """JSON logs to stdout. Bind context with ``structlog.contextvars.bind_contextvars(
    session_id=..., agent_id=..., task_key=..., step=...)``. Never log secrets or
    image bytes."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        cache_logger_on_first_use=True,
    )
