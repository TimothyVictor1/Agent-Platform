"""State shared by every model client built in one process.

Create one ``SharedModelState`` in ``main.py`` (T13) and pass it to ``build_client``
for every role, so that

* all clients talking to the same server share one concurrency semaphore (six agents
  on one local GPU server are limited together, SPEC §8.1), and
* a server found to reject ``tool_choice="required"`` is remembered once for all
  clients using that base URL.

This is the "explicit singleton created in main.py and injected" allowed by
AGENTS.md §3; there is no module-level instance.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import structlog

__all__ = ["SharedModelState"]

log = structlog.get_logger(__name__)


@dataclass
class SharedModelState:
    no_required_tool_choice: set[str] = field(default_factory=set)
    """Base URLs whose server rejected ``tool_choice="required"``."""
    _semaphores: dict[str, tuple[int, asyncio.Semaphore]] = field(default_factory=dict)

    def semaphore(self, key: str, limit: int) -> asyncio.Semaphore:
        """The semaphore for ``key``, created with ``limit`` on first use.

        A later caller asking for a different limit gets the existing semaphore (the
        first configured limit wins) and a warning is logged.
        """
        existing = self._semaphores.get(key)
        if existing is None:
            sem = asyncio.Semaphore(limit)
            self._semaphores[key] = (limit, sem)
            return sem
        first_limit, sem = existing
        if first_limit != limit:
            log.warning(
                "model.concurrency_limit_conflict", key=key, kept=first_limit, ignored=limit
            )
        return sem
