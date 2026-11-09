# Coordination protocol

## Board, digest and coordination tools

Written by T14.

## Scheduling

The scheduler (`runtime/scheduler.py`) runs one session's task DAG on an agent
pool (`runtime/pool.py`), following SPEC §12. It talks to everything else only
through events, work queues and leases. `SPEC.md` stays the source of truth.

### Who emits what

| Component | Emits |
|---|---|
| `Scheduler` (actor `system`) | `task.created`, `task.ready`, `task.assigned`, `task.accepted` (auto review only), `task.failed` (lease expiry), `task.cancelled`, `supervisor.triggered` (`task_failed`), `session.completed`, `session.failed`, `session.stopped`, `session.budget_warning`, `session.budget_exceeded` |
| `AgentPool` (actor `system`) | `agent.created`, `agent.sandbox_booting`, `agent.sandbox_ready`, `agent.status_changed` outside a run (booting, idle, error, stopped), `agent.error` (boot, recovery, ack timeout, lease expiry), `agent.stopped` |
| `AgentWorker` (actor = the agent) | `task.assignment_acked`, `task.started`, then `task.result_submitted` or `task.failed` from the `TaskOutcome` |
| Agent loop (`AgentRuntime`) | `agent.step`, `agent.status_changed` during a run, `agent.stuck_detected`, `agent.error`; bus-only heartbeats and thumbnails (see `agent-loop.md`) |

The scheduler never emits `session.created`: the event store creates the session
row from that event, so the session must exist before the scheduler starts. It
emits only events; the read-model tables (`tasks`, `agents`) are projected by
the runtime wiring (T13).

### Lifecycle

1. **Approval.** When the scheduler sees `session.approved` (plan version `v`),
   it creates the agent pool once (`agent_count` agents; 1 in `solo` mode) and
   emits `task.created` for every task of version `v` whose key does not exist
   yet. Agents get the deterministic names and colors from
   `contracts.agent.agent_identity`. Sandboxes boot concurrently, at most
   `boot_concurrency` (3) at a time, with `boot_attempts` (2) tries each. An agent
   becomes `idle` when its sandbox is ready.
2. **Follow-ups.** A later `session.approved` for a new version adds only the new
   keys, as `pending` tasks with that `plan_version`. They are scheduled on the
   existing agents: no agent or sandbox is re-created. Changes to existing keys in
   later versions are ignored.
3. **Ready set.** A `pending` task whose dependencies are all `done` gets
   `task.ready`. It is recomputed after every event.
4. **Assignment.** Each ready task is paired with an idle agent (status `idle`,
   no assignment, pool-side available). The scheduler takes the best pair first,
   ranked by:
   1. not an agent that already timed out on this task;
   2. the agent named by `task.rework_requested`;
   3. **affinity**: how many of the task's dependencies this agent completed;
   4. **critical path**: number of unfinished descendants;
   5. plan order, then agent order.

   Assignment pauses while the session is `paused`.
5. **Delivery.** `task.assigned` names exactly one `agent_id` and carries `attempt`
   (`task.attempt + 1`), `ack_deadline` (now + 10 s) and `lease_expires_at`. The
   event is persisted first. Then the scheduler takes the task lease and enqueues
   the event **only** on `agent_queue(agent_id)`. It is never broadcast.
6. **Run.** The agent's worker dequeues the assignment and asks the scheduler
   whether it is still current (`work_order`). It then confirms it holds the lease
   (`renew`) and emits `task.assignment_acked`. It pre-fetches upstream artifacts
   (`ArtifactPrefetch`, no-op until T15), emits `task.started` and calls
   `AgentRuntime.run_task(task, upstream)`. `upstream` holds the values of the
   task's `InputRef`s (from upstream `TaskResult.outputs`) and the artifacts that
   upstream tasks produced (from `artifact.published`).
7. **Result.** `submitted` → `task.result_submitted` (status `in_review`). `failed`
   or an agent-level `stopped` → `task.failed` with `will_retry` from the retry
   policy. `lease_lost` → nothing: the task already belongs to someone else. The
   worker releases the lease after reporting.
8. **Review.** `SchedulerConfig.review = "auto"` (the default until T17): the
   scheduler emits `task.accepted` right after `task.result_submitted`, which makes
   the task `done`. With `review = "supervisor"` (T17 switches to it), the
   scheduler waits for the Supervisor's `task.accepted` or `task.rework_requested`.
   A rework sends the task back to `pending`, prefers the same agent and does not
   use up an attempt.

Task states as the scheduler moves them:

```
pending ─► ready ─► assigned ─► running ─► in_review ─► done
   ▲         ▲         │           │
   │         └─────────┘ ack timeout (same attempt)
   ├─────────────────────────────── task.failed will_retry=true / rework
   └► failed (task.failed will_retry=false)       any unfinished ─► cancelled (stop)
```

### Acks and leases

- **Ack timeout (10 s).** If no `task.assignment_acked` arrives before
  `ack_deadline`, the scheduler does four things. It releases the lease and emits
  `agent.error` for the silent agent. It emits `task.ready`; the attempt number is
  unchanged, because the task never started. Finally it starts recovery of that
  agent, which includes a fresh queue consumer. The task goes to another agent if
  one is idle; the silent one is only used as a last resort.
- **Lease.** The key is `task:{session_id}:{task_id}` and the owner is
  `{agent_id}:{task.assigned event id}`, unique per assignment. That way a stale
  delivery or a late worker can never renew a newer assignment's lease. TTL is
  `lease_ttl_s` = 45 s, renewed by the agent runtime's heartbeat every 10 s. The
  pool passes the real TTL into `AgentRuntimeConfig.lease_ttl_s`, so a renewal
  that fails briefly is tolerated while the last renewal still covers the next
  one.
- **Expiry** (runtime crash, hung run, lost sandbox). On each tick the scheduler
  lists expired task leases. For an expired lease that is still the current
  assignment it:
  1. emits `task.failed(reason="lease_expired: …", will_retry=…)`, so the task goes
     back to `ready` and its next assignment has `attempt + 1`;
  2. emits `agent.error` and marks the agent `error`;
  3. starts recovery: fence the old run (it notices the lost lease within one
     heartbeat; it is cancelled after `fence_grace_s`), reconnect to the sandbox
     (`provider.connect`), otherwise re-create it, build a new runtime with the same
     `AgentState`, then back to `idle`. After `max_recoveries` (3) failed recoveries
     the agent is given up.
- **Stale events are ignored.** `task.started`, `task.result_submitted` and
  `task.failed` count only if they come from the current assignment (same agent
  and attempt).

### Retries, failure and completion

- **Retries.** An attempt is retried while
  `attempt - rework_count < max_attempts` (default 2). Failures with reason
  `budget` are never retried. When attempts run out the task becomes `failed` and
  the scheduler emits `supervisor.triggered(reason="task_failed")` once per task.
- **Stall.** Unfinished tasks exist, but none is ready, assigned, running or in
  review, and every pending task depends (transitively) on a failed or missing
  task. With `SchedulerConfig.supervisor_enabled = False` (the default until T17),
  a stall ends the session: `session.failed` (reason lists the failed tasks). Then
  the remaining tasks get `task.cancelled` and the pool shuts down. With
  `supervisor_enabled = True` the scheduler waits for the Supervisor to replan,
  cancel or ask the user.
- **No agents.** If every agent has been given up while work remains, the session
  fails with `session.failed`.
- **Completion.** When every task is `done` or `cancelled` the scheduler emits
  `session.completed`. The scheduler keeps running and waits for follow-ups. Agents
  idle for `idle_timeout_s` (300 s) with no unfinished work are parked: the sandbox
  is paused, or killed (`agent.stopped`, reason `idle timeout`) if the provider
  cannot pause. A follow-up resumes a paused sandbox; for a killed one it boots a
  new sandbox.

### Budgets

`SessionBudget` is one `BudgetMonitor` shared by every agent runtime. It covers
total steps, tokens, cost and wall time; wall time counts from the first
`session.approved`. On restart it is seeded from the `agent.step` usage in the
log.

- **80 %** of a limit: one `session.budget_warning` per metric.
- **100 %**: `session.budget_exceeded`. The runtimes stop at their next step check
  and fail their task with reason `budget` (no retry). The scheduler waits up to
  `stop_grace_s` for them, cancels every other unfinished task, then emits
  `session.stopped(reason="budget")` and shuts the pool down.
- Budget stops are checked before stalls, so a budget failure never turns into
  `session.failed`.

### Stop

`Scheduler.stop("user")` (called by T13 for `command.stop`):

1. sets every agent's control to `stop` and waits for the runs (cancelled after
   `stop_grace_s`);
2. cancels every unfinished task;
3. emits `session.stopped`, but not if the session already completed or failed;
4. kills the sandboxes and emits `agent.stopped`.

`run()` then returns. If the session ends elsewhere (`session.stopped` or
`session.failed` in the log), the scheduler does the same steps without emitting
a second session event.

### Restarts and ordering

The scheduler's state is a fold over the session's events (`SchedulerState.apply`).
Events are applied strictly in `seq` order:

- events the scheduler records itself are applied immediately;
- the rest come from the bus stream;
- any gap, and every tick, is filled from `EventStore.read`.

A new runtime builds a new pool and scheduler on the same store, bus and leases
and calls `run()`. Known agents are adopted: their sandboxes are reconnected, or
re-created if gone. Orphaned assignments resolve through the ack timeout or lease
expiry. Cancelling `run()` (runtime shutdown) closes the workers but leaves the
sandboxes alive for the next runtime.

### Configuration

| `SchedulerConfig` | Default | | `PoolConfig` | Default |
|---|---|---|---|---|
| `ack_timeout_s` | 10 | | `boot_concurrency` | 3 |
| `lease_ttl_s` | 45 | | `boot_attempts` | 2 |
| `tick_s` | 1 | | `max_recoveries` | 3 |
| `idle_timeout_s` | 300 | | `fence_grace_s` | 5 |
| `stop_grace_s` | 30 | | `stop_grace_s` | 30 |
| `budget_warning_ratio` | 0.8 | | `screen` | 1280×800 |
| `review` | `"auto"` | | `network` / `allowlist` | `open` / none |
| `supervisor_enabled` | `False` | | | |

### Wiring (T13)

T13 supplies a `RuntimeFactory(session, agent, sandbox, deps) -> AgentRuntime`
that builds the model, registry, digest, board and approvals. It must pass `deps`
through to `AgentRuntime`: `control`, `budget`, `renew_lease` and `state`. The pool
enforces `config.lease_ttl_s == deps.lease_ttl_s`. Mode rules are applied by
`AgentRuntime`: `parallel_isolated` disables the `coordination` and `artifacts`
tool groups and uses empty digests. Sandbox specs never carry secrets
(`env` is empty).
