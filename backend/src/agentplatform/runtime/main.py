"""``agentplatform-runtime``: the runtime process (T13; SPEC §5.1, §12, §16).

``Runtime`` owns sessions through a lease in the ``sessions`` table
(``SessionDirectory.claim``), so two runtimes never run the same session. For each
owned session a ``SessionHost`` runs, as asyncio tasks:

* the ``Scheduler`` (supervised), the ``Supervisor`` and the session watcher
  (``container.SessionRuntime``);
* the command loop: commands from ``session_command_queue(session_id)`` (and
  plan requests handed over by the runtime) go one at a time through
  ``CommandRouter``; a command is acked once handled, so a crash redelivers it;
* the lease renewer. A runtime that cannot renew a lease stops the session's work
  at once without touching its sandboxes (fenced): another runtime adopts it.

Process loops:

* ``NEW_SESSIONS_QUEUE`` (``command.plan_session``): claim the session, then plan
  it in its host. A session another runtime holds is left to that runtime.
* Startup recovery: every adoptable session (planning, awaiting approval, running,
  paused, and completed within ``APP_ADOPT_COMPLETED_HOURS``) whose lease is free
  is claimed and hosted: control holds are restored from Redis, approvals nobody
  waits for any more are expired, and the scheduler adopts the agents (reconnects
  their sandboxes via ``SandboxRef``, else re-creates them) and re-leases tasks
  (orphaned assignments resolve through the ack timeout or lease expiry).
* Sweeps: sessions stuck in ``planning`` (``session.created`` committed but the
  plan command never enqueued or lost) are planned; orphaned ``docker_local``
  sandboxes are removed at startup and every ``APP_ORPHAN_SWEEP_MIN``.

Shutdown (SIGTERM/SIGINT): stop taking work, stop the supervisors, cancel the
schedulers (the pool closes workers and runs but leaves sandboxes alive), then
release the session leases so another runtime can adopt the sessions at once.
Agent state is event-sourced; nothing else needs a checkpoint. Task leases are left
to expire (45 s), after which the adopting scheduler retries the task.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from datetime import datetime, timedelta

import structlog

from agentplatform.bus.base import NEW_SESSIONS_QUEUE, session_command_queue
from agentplatform.config import find_repo_root, load_settings
from agentplatform.contracts.commands import CommandBase, PlanSessionCommand
from agentplatform.contracts.common import Actor, utcnow
from agentplatform.contracts.events import EventDraft, SessionFailed
from agentplatform.contracts.session import Session
from agentplatform.errors import ConfigError
from agentplatform.logs import configure_logging
from agentplatform.models.errors import redact_secrets
from agentplatform.runtime.commands import CommandRouter, read_plan_log
from agentplatform.runtime.container import (
    Container,
    SessionRuntime,
    build_container,
    probe_spec,
    stop_quietly,
)
from agentplatform.runtime.policies import check_secret_hygiene_async

__all__ = ["Runtime", "SessionHost", "main"]

log = structlog.get_logger(__name__)

_ENDED = frozenset({"failed", "stopped"})
_COMMAND_RETRY_S = 1.0
_COMMAND_DRAIN_S = 10.0
_UNKNOWN_SESSION_ATTEMPTS = 20
"""Deliveries of a plan command for a session this runtime's database does not know
before it is dropped (about 10 minutes with the 30 s visibility timeout)."""


def safe_error_reason(exc: Exception, secrets: Sequence[str], *, limit: int = 500) -> str:
    """``exc`` as a message safe for events and logs: every configured secret value and
    anything that looks like a key replaced by ``[redacted]``, length capped."""
    text = f"{type(exc).__name__}: {exc}"
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[redacted]")
    return redact_secrets(text)[:limit]


class SessionHost:
    """Everything a runtime runs for one owned session (module docstring)."""

    def __init__(self, runtime: Runtime, session: Session) -> None:
        self.runtime = runtime
        self.session = session
        self.rt = SessionRuntime(runtime.container, session)
        self.router = CommandRouter(self.rt)
        self.plan_requests: asyncio.Queue[str | None] = asyncio.Queue()
        self.done = asyncio.Event()
        self.fenced = False
        self._idle = asyncio.Event()
        self._idle.set()
        self._tasks: list[asyncio.Task[None]] = []
        self._main: asyncio.Task[None] | None = None
        self._log = log.bind(session_id=session.id)

    @property
    def id(self) -> str:
        return self.session.id

    async def start(self, *, adopted: bool) -> None:
        c = self.runtime.container
        await self.rt.controls.restore()
        if adopted:
            expired = await c.approvals.expire_pending(self.id, note="runtime restarted")
            if expired:
                self._log.info("approvals_expired_on_adopt", count=len(expired))
            plan_log = await read_plan_log(c.store, self.id)
            if plan_log.status == "paused" and not self.rt.controls.session_paused:
                self.rt.controls.pause_session()  # control state was lost; the log wins
        self._main = asyncio.create_task(self._run(), name=f"session-{self.id}")

    async def _run(self) -> None:
        rt = self.rt
        spawn = asyncio.create_task
        self._tasks = [
            spawn(self._commands(), name=f"commands-{self.id}"),
            spawn(self._renew(), name=f"lease-{self.id}"),
            spawn(rt.watch(), name=f"watch-{self.id}"),
        ]
        if rt.supervisor is not None:
            self._tasks.append(spawn(rt.supervisor.run(), name=f"supervisor-{self.id}"))
        try:
            await rt.scheduler.run()  # returns when the session stopped or failed
            self._log.info("session_ended")
            await self._finish(ended=True)
        except asyncio.CancelledError:
            await self._finish(ended=False)
            raise
        except Exception:
            self._log.exception("scheduler_crashed")
            await self._finish(ended=False)

    async def _finish(self, *, ended: bool) -> None:
        if self.rt.supervisor is not None:
            await self.rt.supervisor.stop()
        if ended:
            # e.g. the stop command that ended the session: let it finish and be acked
            with suppress(TimeoutError):
                await asyncio.wait_for(self._idle.wait(), _COMMAND_DRAIN_S)
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                await stop_quietly(task)
        if ended:
            await self.rt.controls.clear()
        if not self.fenced:
            await self.runtime.container.directory.release(self.id, self.runtime.owner)
        self.done.set()

    async def close(self) -> None:
        """Shutdown or fence: stop everything, keep sandboxes, release the lease
        (unless fenced)."""
        await self.rt.controls.flush()
        await stop_quietly(self._main)
        if not self.done.is_set():
            await self._finish(ended=False)

    # -- loops ----------------------------------------------------------------------

    async def _commands(self) -> None:
        c = self.runtime.container
        queue = session_command_queue(self.id)
        while True:
            try:
                request = self.plan_requests.get_nowait()
            except asyncio.QueueEmpty:
                pass
            else:
                self._idle.clear()
                try:
                    await self._guarded(self.router.plan_initial(request), "plan")
                finally:
                    self._idle.set()
                continue
            delivery = await c.bus.dequeue(
                queue, self.runtime.owner, timeout_s=c.knobs.command_dequeue_timeout_s
            )
            if delivery is None:
                continue
            self._idle.clear()
            try:
                item = delivery.item
                if not isinstance(item, CommandBase):
                    self._log.warning("unexpected_queue_item")
                    await c.bus.ack(delivery)
                elif await self._guarded(self.router.handle(item), item.type):
                    await c.bus.ack(delivery)
            finally:
                self._idle.set()

    async def _guarded(self, work: Awaitable[None], what: str) -> bool:
        """Run ``work``; infrastructure errors are logged and the command stays
        unacked (redelivered after the visibility timeout)."""
        try:
            await work
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.error("command_failed", what=what, error=type(exc).__name__)
            await asyncio.sleep(_COMMAND_RETRY_S)
            return False

    async def _renew(self) -> None:
        c = self.runtime.container
        knobs = c.knobs
        while True:
            await asyncio.sleep(knobs.session_lease_renew_s)
            try:
                ok = await c.directory.renew(self.id, self.runtime.owner, knobs.session_lease_ttl_s)
            except Exception as exc:
                self._log.warning("session_lease_renew_failed", error=type(exc).__name__)
                continue  # the lease is still valid until its TTL; try again
            if not ok:
                self._log.error("session_lease_lost")
                self.fenced = True
                self.runtime.fence(self.id)
                return


class Runtime:
    """The runtime process (module docstring). ``start`` recovers sessions and starts
    the loops; ``shutdown`` is graceful; ``crash`` (tests) drops everything without
    releasing anything, like a killed process."""

    def __init__(self, container: Container, *, now: Callable[[], datetime] = utcnow) -> None:
        self.container = container
        self.owner = container.knobs.runtime_id
        self.hosts: dict[str, SessionHost] = {}
        self._now = now
        self._loops: list[asyncio.Task[None]] = []
        self._closing = False
        self._log = log.bind(runtime_id=self.owner)

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        c = self.container
        await check_secret_hygiene_async(
            c.secrets, [probe_spec()], repo_root=find_repo_root()
        )  # raises ConfigError on secrets in sandbox env; warns when .env is tracked
        await self.cleanup_orphans()
        await self.recover()
        spawn = asyncio.create_task
        self._loops = [
            spawn(self._new_sessions(), name="new-sessions"),
            spawn(self._sweeps(), name="sweeps"),
        ]
        self._log.info("runtime_started", sessions=len(self.hosts))

    async def shutdown(self) -> None:
        self._closing = True
        for loop in self._loops:
            await stop_quietly(loop)
        await asyncio.gather(*(h.close() for h in list(self.hosts.values())))
        self.hosts.clear()
        self._log.info("runtime_stopped")

    async def crash(self) -> None:
        """Test helper: stop every task without releasing leases or closing anything
        gracefully (what a killed process leaves behind)."""
        self._closing = True
        for loop in self._loops:
            await stop_quietly(loop)
        for host in self.hosts.values():
            host.fenced = True  # keep the lease: it must expire
            await stop_quietly(host._main)
        self.hosts.clear()

    # -- sessions -------------------------------------------------------------------

    async def host(self, session: Session, *, adopted: bool) -> SessionHost | None:
        """Claim ``session`` and start its host (no-op if already hosted)."""
        existing = self.hosts.get(session.id)
        if existing is not None and not existing.done.is_set():
            return existing
        c = self.container
        if not await c.directory.claim(session.id, self.owner, c.knobs.session_lease_ttl_s):
            return None
        host = SessionHost(self, session)
        self.hosts[session.id] = host
        await host.start(adopted=adopted)
        self._log.info("session_hosted", session_id=session.id, adopted=adopted)
        return host

    def fence(self, session_id: str) -> None:
        host = self.hosts.pop(session_id, None)
        if host is not None:
            asyncio.get_running_loop().create_task(host.close(), name=f"fence-{session_id}")

    async def recover(self) -> None:
        c = self.container
        since = self._now() - timedelta(seconds=c.knobs.adopt_completed_s)
        for session in await c.directory.adoptable(self.owner, completed_since=since):
            try:
                await self.host(session, adopted=True)
            except Exception as exc:
                self._log.exception(
                    "session_adopt_failed", session_id=session.id, error=type(exc).__name__
                )

    async def plan_stuck(self) -> list[str]:
        """Plan sessions stuck in ``planning`` (no plan command ever handled)."""
        c = self.container
        cutoff = self._now() - timedelta(seconds=c.knobs.planning_sweep_s)
        planned: list[str] = []
        for session in await c.directory.stuck_planning(created_before=cutoff):
            host = await self.host(session, adopted=True)
            if host is not None:
                self._log.warning("stuck_planning_recovered", session_id=session.id)
                host.plan_requests.put_nowait(None)
                planned.append(session.id)
        for host in list(self.hosts.values()):  # owned by us but never planned
            if host.session.created_at < cutoff and not host.done.is_set():
                plan_log = await read_plan_log(c.store, host.id)
                idle = host.plan_requests.empty() and host.id not in planned
                if plan_log.status == "planning" and not plan_log.planned and idle:
                    host.plan_requests.put_nowait(None)
                    planned.append(host.id)
        return planned

    async def cleanup_orphans(self) -> list[str]:
        c = self.container
        if not c.orphan_cleaners:
            return []
        since = self._now() - timedelta(seconds=c.knobs.adopt_completed_s)
        try:
            alive = await c.directory.alive_ids(completed_since=since)
        except Exception as exc:
            self._log.warning("orphan_cleanup_skipped", error=type(exc).__name__)
            return []
        removed: list[str] = []
        for cleaner in c.orphan_cleaners:
            try:
                removed.extend(await cleaner(alive))
            except Exception as exc:
                self._log.warning("orphan_cleanup_failed", error=type(exc).__name__)
        if removed:
            self._log.info("orphans_removed", count=len(removed))
        return removed

    # -- loops ----------------------------------------------------------------------

    async def _new_sessions(self) -> None:
        c = self.container
        while not self._closing:
            delivery = await c.bus.dequeue(
                NEW_SESSIONS_QUEUE, self.owner, timeout_s=c.knobs.command_dequeue_timeout_s
            )
            if delivery is None:
                continue
            item = delivery.item
            if not isinstance(item, PlanSessionCommand):
                self._log.warning("unexpected_new_session_item")
                await c.bus.ack(delivery)
                continue
            session: Session | None = None
            try:
                session = await c.directory.get(item.session_id)
                host = None if session is None else await self.host(session, adopted=False)
            except ConfigError as exc:
                # Permanent (e.g. a model key that is not set): a retry cannot help, so
                # fail the session now instead of leaving it in ``planning``.
                if session is not None and await self._fail_unhostable(session, exc):
                    await c.bus.ack(delivery)
                else:
                    self._log.error("new_session_failed", error=type(exc).__name__)
                    await asyncio.sleep(_COMMAND_RETRY_S)
                continue
            except Exception as exc:
                self._log.error("new_session_failed", error=type(exc).__name__)
                await asyncio.sleep(_COMMAND_RETRY_S)
                continue  # not acked: redelivered
            if session is None and delivery.attempt < _UNKNOWN_SESSION_ATTEMPTS:
                # Not in our database: the queue may be shared with runtimes of other
                # databases (worktrees on one Redis). Leave it for them (redelivered
                # after the visibility timeout); give up after a few rounds.
                self._log.info("new_session_unknown", session_id=item.session_id)
                continue
            if host is None:
                # unknown for good, or another runtime holds it (its sweep plans it)
                self._log.info("new_session_not_hosted", session_id=item.session_id)
            else:
                host.plan_requests.put_nowait(item.id)
            await c.bus.ack(delivery)

    async def _fail_unhostable(self, session: Session, exc: ConfigError) -> bool:
        """Record ``session.failed`` for a session this runtime cannot host and release
        its lease. ``False`` if that did not work (the command is then retried)."""
        c = self.container
        reason = safe_error_reason(exc, c.secrets)
        self._log.error(
            "new_session_failed", session_id=session.id, error=type(exc).__name__, reason=reason
        )
        try:
            await c.recorder.record(
                session.id,
                EventDraft(payload=SessionFailed(reason=reason), actor=Actor.system()),
            )
            await c.directory.release(session.id, self.owner)
        except Exception as record_exc:
            self._log.error("session_fail_record_failed", error=type(record_exc).__name__)
            return False
        return True

    async def _sweeps(self) -> None:
        c = self.container
        loop = asyncio.get_running_loop()
        next_orphans = loop.time() + c.knobs.orphan_sweep_s
        while not self._closing:
            await asyncio.sleep(c.knobs.sweep_interval_s)
            for host_id in [h for h, host in self.hosts.items() if host.done.is_set()]:
                self.hosts.pop(host_id, None)
            try:
                await self.plan_stuck()
                await self.recover()  # sessions whose owner died meanwhile
            except Exception as exc:
                self._log.warning("sweep_failed", error=type(exc).__name__)
            if loop.time() >= next_orphans:
                next_orphans = loop.time() + c.knobs.orphan_sweep_s
                await self.cleanup_orphans()


async def _amain() -> int:
    settings = load_settings()
    try:
        container = await build_container(settings)
    except ConfigError as exc:
        log.error("runtime_config_error", detail=exc.detail)
        return 2
    runtime = Runtime(container)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await runtime.start()
    except ConfigError as exc:
        log.error("runtime_refused_to_start", detail=exc.detail)
        await container.aclose()
        return 2
    await stop.wait()
    log.info("runtime_shutdown_requested")
    await runtime.shutdown()
    await container.aclose()
    return 0


def main() -> None:
    configure_logging()
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
