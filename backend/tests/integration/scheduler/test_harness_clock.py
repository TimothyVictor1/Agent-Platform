"""Scheduler deadlines, lease expiry and queue redelivery use one virtual timeline."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from agentplatform.bus.base import agent_queue
from agentplatform.runtime.scheduler import lease_key

from .harness import FAST_SCHEDULER, open_session


async def test_ack_deadline_leases_and_queue_redelivery_share_the_clock() -> None:
    w = await open_session(
        agent_count=2,
        deaf_agents=["Ada", "Brook"],
        scheduler_config=replace(FAST_SCHEDULER, ack_timeout_s=100, lease_ttl_s=200),
    )
    try:
        assigned = await w.wait_for("task.assigned")
        clock = w.clock
        initial = clock.now()
        task = w.scheduler.state.tasks["T1"]
        queue = agent_queue(assigned.agent_id)
        delivery = await w.bus.dequeue(queue, "test-consumer", timeout_s=0)
        assert delivery is not None
        lease = await w.leases.get(lease_key(w.session.id, task.id))
        assert lease is not None
        assert lease.expires_at_ms == int(assigned.lease_expires_at.timestamp() * 1000)
        assert assigned.ack_deadline > initial
        # Real scheduling delays do not move the clock. Only harness sleep does.
        await asyncio.sleep(0.05)
        assert clock.now() == initial
        assert await w.leases.acquire("probe", "owner", ttl_s=1)
        await clock.sleep(30.01)
        assert await w.leases.get("probe") is None
        assert [e.key for e in await w.leases.expired("probe")] == ["probe"]
        again = await w.bus.dequeue(queue, "test-consumer", timeout_s=0)
        assert again is not None
        assert again.delivery_id == delivery.delivery_id
        assert again.attempt == 2
        assert await w.payloads("task.ready")
        assert len(await w.payloads("task.assigned")) == 1
        await clock.sleep(70.01)
        await w.wait_for("task.assigned", lambda p: p.agent_id != assigned.agent_id)
        assert clock.now() >= assigned.ack_deadline
        replacement = await w.leases.get(lease_key(w.session.id, task.id))
        assert replacement is not None
        assert replacement.owner != lease.owner
    finally:
        await w.stop()


async def test_host_delays_do_not_spend_ack_and_lease_allowances() -> None:
    w = await open_session(
        agent_count=2,
        deaf_agents=["Ada", "Brook"],
        scheduler_config=replace(FAST_SCHEDULER, ack_timeout_s=0.1, lease_ttl_s=0.2),
    )
    try:
        assigned = await w.wait_for("task.assigned")
        initial = w.clock.now()
        await asyncio.sleep(0.3)  # exceeds both deadlines on the host clock
        assert w.clock.now() == initial
        assert len(await w.payloads("task.assigned")) == 1
        assert await w.payloads("task.failed") == []
        assert await w.leases.get(lease_key(w.session.id, assigned.task_id)) is not None
        await w.sleep(0.11)
        await w.wait_for("task.assigned", lambda p: p.agent_id != assigned.agent_id)
        assert len(await w.payloads("task.assigned")) == 2
    finally:
        await w.stop()
