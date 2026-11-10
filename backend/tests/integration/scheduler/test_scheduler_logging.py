"""Background feeder termination remains visible even with store polling recovery."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from .harness import open_session


@pytest.mark.parametrize("crash", [False, True])
async def test_feeder_exit_while_running_is_logged(
    monkeypatch: pytest.MonkeyPatch, crash: bool
) -> None:
    w = await open_session(start=False)
    error = RuntimeError("subscription failed")

    async def feed() -> None:
        if crash:
            raise error

    monkeypatch.setattr(w.scheduler, "_feed", feed)
    with capture_logs() as logs:
        w.start()
        try:

            async def logged() -> bool:
                return any(e["event"] == "scheduler_feeder_exited" for e in logs)

            await w.until(logged, limit_s=1)
            await w.wait_for("session.completed")  # durable-store catch-up still works
        finally:
            await w.stop()
    exits = [e for e in logs if e["event"] == "scheduler_feeder_exited"]
    assert len(exits) == 1
    assert exits[0]["session_id"] == w.session.id
    if crash:
        assert exits[0]["exc_info"][1] is error


async def test_normal_shutdown_does_not_report_feeder_exit() -> None:
    with capture_logs() as logs:
        w = await open_session()
        await w.wait_for("session.completed")
        await w.stop()
    assert not any(e["event"] == "scheduler_feeder_exited" for e in logs)
