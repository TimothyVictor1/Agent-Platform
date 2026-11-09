"""Per-agent board post rate limits (SPEC §13.1).

Max 1 ``progress`` and max 6 posts of any kind per 10 steps, counted over the
window of the agent's last 10 step numbers (the current step included).

The counters live in memory: they are per process and reset on a runtime restart,
which can only loosen the limit for a moment (a documented v1 trade-off, see
``docs/coordination-protocol.md``). Entries do not store the step, so the limit
cannot be rebuilt from the database.
"""

from __future__ import annotations

from collections import deque

from agentplatform.contracts.common import BoardEntryKind
from agentplatform.errors import RateLimitedError

__all__ = ["MAX_POSTS_PER_WINDOW", "MAX_PROGRESS_PER_WINDOW", "WINDOW_STEPS", "PostRateLimiter"]

WINDOW_STEPS = 10
MAX_PROGRESS_PER_WINDOW = 1
MAX_POSTS_PER_WINDOW = 6


class PostRateLimiter:
    """``check`` before a post, ``record`` after it succeeded."""

    def __init__(self) -> None:
        self._posts: dict[str, deque[tuple[int, BoardEntryKind]]] = {}

    def _window(self, agent_id: str, step: int) -> list[tuple[int, BoardEntryKind]]:
        posts = self._posts.get(agent_id)
        if posts is None:
            return []
        oldest = step - WINDOW_STEPS + 1
        return [(s, k) for s, k in posts if oldest <= s <= step]

    def check(self, agent_id: str, step: int, kind: BoardEntryKind) -> None:
        """Raise ``RateLimitedError`` (a model-friendly message) when posting ``kind``
        at ``step`` would exceed a limit."""
        window = self._window(agent_id, step)
        if len(window) >= MAX_POSTS_PER_WINDOW:
            raise RateLimitedError(
                f"board post limit reached ({MAX_POSTS_PER_WINDOW} posts per {WINDOW_STEPS} "
                "steps); continue with your task and post again in a few steps"
            )
        if kind == "progress" and sum(1 for _, k in window if k == "progress") >= (
            MAX_PROGRESS_PER_WINDOW
        ):
            raise RateLimitedError(
                f"progress post limit reached ({MAX_PROGRESS_PER_WINDOW} per {WINDOW_STEPS} steps)"
            )

    def record(self, agent_id: str, step: int, kind: BoardEntryKind) -> None:
        posts = self._posts.setdefault(agent_id, deque())
        posts.append((step, kind))
        oldest = step - WINDOW_STEPS + 1
        while posts and posts[0][0] < oldest:
            posts.popleft()
