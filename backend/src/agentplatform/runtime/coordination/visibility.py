"""Who may see a board entry (SPEC §13.2).

One predicate shared by ``BoardService.search``/``entries_after`` and the digest
builder. It matches ``tools/control.py:_visible_to`` (used by ``wait_for``); a unit
test pins the two together.
"""

from __future__ import annotations

from agentplatform.contracts.board import BoardEntry

__all__ = ["is_own", "visible_to"]


def is_own(entry: BoardEntry, agent_id: str) -> bool:
    """``True`` when ``agent_id`` authored ``entry``."""
    return entry.author.type == "agent" and entry.author.id == agent_id


def visible_to(entry: BoardEntry, agent_id: str) -> bool:
    """Broadcast entries (``to is None``), direct entries addressed to the agent, and
    the agent's own entries. Direct messages are invisible to everybody else."""
    return entry.to is None or agent_id in entry.to or is_own(entry, agent_id)
