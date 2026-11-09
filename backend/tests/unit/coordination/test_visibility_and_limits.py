"""Visibility predicate and the in-memory post rate limiter (no database)."""

from __future__ import annotations

import pytest

from agentplatform.contracts.common import Actor
from agentplatform.errors import RateLimitedError
from agentplatform.runtime.agent.tools.control import _visible_to as control_visible_to
from agentplatform.runtime.coordination.ratelimit import PostRateLimiter
from agentplatform.runtime.coordination.visibility import is_own, visible_to
from agentplatform.testing.builders import make_board_entry

ADA, BROOK = Actor.agent("a-ada", "Ada"), Actor.agent("a-brook", "Brook")


def test_visibility_rules() -> None:
    broadcast = make_board_entry(author=BROOK)
    direct = make_board_entry("note", author=BROOK, to=["a-cleo"])
    own_direct = make_board_entry("note", author=ADA, to=["a-cleo"])
    assert visible_to(broadcast, "a-ada")
    assert not visible_to(direct, "a-ada")
    assert visible_to(direct, "a-cleo")
    assert visible_to(own_direct, "a-ada")  # authors see what they sent
    assert is_own(own_direct, "a-ada")
    assert not is_own(direct, "a-ada")
    # a supervisor/user entry whose id equals an agent id is not "own"
    spoof = make_board_entry("note", author=Actor.user("a-ada"), to=["a-cleo"])
    assert not visible_to(spoof, "a-ada")


@pytest.mark.parametrize("viewer", ["a-ada", "a-brook", "a-cleo", "a-zed"])
def test_matches_the_visibility_wait_for_enforces(viewer: str) -> None:
    entries = [
        make_board_entry(author=BROOK),
        make_board_entry("note", author=BROOK, to=["a-cleo"]),
        make_board_entry("note", author=ADA, to=["a-cleo", "a-brook"]),
        make_board_entry("note", author=Actor.supervisor(), to=["a-zed"]),
        make_board_entry("note", author=Actor.user("a-ada"), to=["a-brook"]),
    ]
    for entry in entries:
        assert visible_to(entry, viewer) == control_visible_to(entry, viewer)


def test_progress_limit_is_one_per_ten_steps() -> None:
    limiter = PostRateLimiter()
    limiter.check("a", 1, "progress")
    limiter.record("a", 1, "progress")
    limiter.check("a", 5, "fact")  # other kinds are fine
    with pytest.raises(RateLimitedError, match="progress"):
        limiter.check("a", 10, "progress")  # steps 1..10 is one window
    limiter.check("a", 11, "progress")  # step 1 left the window


def test_six_posts_per_ten_steps_and_per_agent() -> None:
    limiter = PostRateLimiter()
    for step in (1, 2, 3, 4, 5, 6):
        limiter.check("a", step, "fact")
        limiter.record("a", step, "fact")
    with pytest.raises(RateLimitedError, match="post limit"):
        limiter.check("a", 7, "note")
    limiter.check("b", 7, "note")
    limiter.check("a", 11, "note")  # steps 1 left the window, 5 posts remain


def test_check_does_not_record() -> None:
    limiter = PostRateLimiter()
    for _ in range(20):
        limiter.check("a", 1, "progress")
