# Agent step loop

`AgentRuntime` (`backend/src/agentplatform/runtime/agent/loop.py`) runs **one agent
on one task at a time** (SPEC §10). It is coded only against interfaces, so later
tasks plug in without editing it. `SPEC.md` stays the source of truth.

| Module | What it does |
|---|---|
| `runtime/agent/loop.py` | `AgentRuntime`, `TaskOutcome`, `ToolContext`, the plug-in Protocols |
| `runtime/agent/context.py` | `ContextBuilder`: system prompt, per-step messages, history, budget trim |
| `runtime/agent/prompts/agent_system.md` | System prompt template (`string.Template` placeholders) |
| `runtime/agent/stuck.py` | `StuckDetector` (four signals of §10.6, plus `no_tool`) |
| `runtime/agent/tools/desktop.py` | click, move, drag, scroll, type_text, key, run_command, wait, zoom |
| `runtime/agent/tools/control.py` | submit_result, fail_task, request_approval, wait_for |

## Event ownership (T11 and T13 must follow this)

| Who | Emits |
|---|---|
| **Agent loop** | `agent.step`, `agent.status_changed`, `agent.stuck_detected`, `agent.error`; bus-only `agent.heartbeat` and thumbnails |
| **Scheduler / runtime wiring (T11/T13)** | `task.started` (before calling `run_task`), then from the returned `TaskOutcome`: `task.result_submitted` (status `submitted`) or `task.failed` (status `failed`, with `will_retry` from its retry policy); `agent.stopped` when it tears an agent down |

`run_task` never emits `task.*` events. It returns:

```python
TaskOutcome(status="submitted" | "failed" | "stopped",
            result: TaskResult | None,   # set when submitted
            reason: FailureReason | None, # set when failed
            detail: str, steps: int, usage: Usage)
```

| `reason` | Meaning | Suggested scheduler reaction |
|---|---|---|
| `fail_task` | The model gave up (`detail` holds its reason and what it tried) | retry or hand to the Supervisor |
| `budget` | `max_steps_per_task` or a session budget (`BudgetMonitor.exceeded()`) was hit | fail with reason `budget` (§12.8) |
| `model_error` | 3 consecutive model failures | retry later |
| `sandbox_lost` | `SandboxExpiredError` while observing | re-create/reconnect sandbox, requeue |
| `sandbox_error` | 3 consecutive other sandbox failures | same |
| `lease_lost` | `renew_lease` returned False, raised, or timed out (fail closed; see below) | none: someone else owns the task now |

`status="stopped"` means the user's stop (`ControlState.mode == "stop"`).
After `run_task` returns, the agent's status is `idle` (submitted or failed),
`stopped` (stopped) or `error` (lease lost). `run_task` raises only if the event
store fails; treat that like a runtime crash (the lease expires and the task is
requeued).

## Plug-in interfaces

All are Protocols in `loop.py`. Every one has a default, so a bare `AgentRuntime`
works in tests.

| Interface | Default | Real one |
|---|---|---|
| `DigestProvider.build(agent, task, since=, budget_tokens=) -> Digest(text, cursor)` | `NoDigest` | T14 (`coordination/digest.py`) |
| `ControlState.mode` / `wait_for_change(current)` | `LocalControlState` (in-process) | T13 (Redis key + in-process event) |
| `ApprovalGate.request(...) -> Approval` (decided or expired) | none: `request_approval` returns an `ERROR` | T18 |
| `BudgetMonitor.record(usage)` / `exceeded()` | `LocalBudget(session.budgets)` | T11 (session-wide, shared by agents) |
| `RosterProvider.roster() -> [RosterEntry]` | just this agent | T13 |
| `renew_lease() -> bool` | none | T11/T13 (`LeaseStore.renew`) |
| `Summarizer.summarize(previous, lines, task=)` | `TruncatingSummarizer` | `ModelSummarizer(summarizer model)` |
| `fallback_parser(text, tool_names)` | none | T02 (`models/fallback_parser.py`) |
| `ToolRegistry` | `build_agent_registry()` (desktop + control) | T13 adds coordination (T14) and artifact (T15) tools |

`EventSink` is `EventRecorder`, `EphemeralPublisher` is `EventBus`, and frames go to
an `ArtifactStore`. `AgentRuntimeConfig.from_role(profile.agent)` maps the model
profile (coordinate mode, screenshot width, image window, prompt budget,
`image_tokens`).

Tools from other tasks receive a `ToolContext`: session id, agent, task, step,
`state: AgentState` (digest cursor, `notebook`, total steps, status), sandbox, this
step's observation and PNG, coordinate mode, `frame_id`, board, artifact store,
approval gate and `hooks`. `hooks` can set the status, run `wait_or_stop(aw)`
(returns `None` if a stop or lease loss interrupts), `sleep`, and `add_note(text)`
for the next step. `notebook_write` (T14) writes `ctx.state.notebook`.

## One step

```mermaid
sequenceDiagram
    participant S as Scheduler (T11/T13)
    participant L as AgentRuntime
    participant C as ControlState
    participant X as Sandbox
    participant A as ArtifactStore
    participant D as DigestProvider
    participant M as ModelClient
    participant R as ToolRegistry
    participant E as EventRecorder / Bus

    S->>L: run_task(task, upstream)
    Note over L: heartbeat task: every 10 s Heartbeat (bus) + renew_lease()
    loop until submitted / failed / stopped
        L->>C: checkpoint (pause/takeover waits; stop exits)
        L->>L: budget check (max_steps_per_task, BudgetMonitor)
        L->>X: screenshot()
        L->>L: prepare_observation (resize, JPEG, pHash)
        L->>A: put small frame (480 px, q50) [+ full frame by policy]
        L-->>E: Thumbnail (bus only)
        L->>D: build(since=cursor, 700 tokens)
        L->>M: complete(messages, tools, tool_choice=required)  [raced with stop]
        alt no tool call
            L->>M: retry ≤2 with corrective message, then stuck(no_tool)
        end
        L->>L: check composition (≤2 coordination, then 1 desktop/control last)
        L->>C: re-check mode (drop the action if user took over)
        L->>R: execute coordination calls, then the terminal call
        L-->>E: agent.step (calls, reasoning ≤400 chars, frame, usage)
        L->>L: StuckDetector.observe → agent.stuck_detected (once per episode)
        L->>L: history append; summarize every 20 steps
    end
    L-->>S: TaskOutcome
    S-->>E: task.result_submitted / task.failed
```

Step rules the loop enforces:

- **Composition.** Up to 2 coordination calls, then exactly one desktop or control
  call, which must be last. If the rule is broken, nothing in the step runs and
  every call gets `ERROR: not executed: …` with the rule, so the model can fix it.
  Unknown tools get the registry's `ERROR … Valid tools: …`.
- **Phase gating.** `submit_result` is hidden, and rejected if called anyway, for
  steps 1–3.
- **Tool results** are at most 600 chars. Failures come back as `ERROR: …` and
  never crash the loop. Unexpected exceptions are reported by type only, so no
  internal details leak to the model.
- **Step counting.** A step counts once the model has answered. A failed model
  call or screenshot is retried without consuming a step.

## Control, stop and leases

- **Pause / takeover**: at the checkpoint the status becomes `paused_by_user` and
  the loop waits. On resume the next step gets the system note *"The user
  interacted with your screen. Re-observe carefully before acting."*
- **Pause during a model call**: the step's actions are **not executed** (the mode
  is checked again right before execution). The results say why, and the loop
  pauses at the next checkpoint.
- **Stop**: `wait_or_stop` races the model call, `wait`, `request_approval` and
  `wait_for` against the stop signal, so the loop exits within one step and the
  pending call is cancelled.
- **Lease**: the heartbeat task publishes `Heartbeat` and calls `renew_lease()`
  every `heartbeat_interval_s` (10 s), including while paused or waiting for an
  approval. If the lease is lost, everything in flight is interrupted (also while
  paused) and the task ends with `lease_lost`. Renewal fails closed: an exception
  or a timeout (`lease_renew_timeout_s`, 5 s) counts as lease loss, unless
  `lease_ttl_s` is configured and the last successful renewal still covers the
  next attempt. The heartbeat publish is bounded by `heartbeat_publish_timeout_s`
  (2 s), so a hung bus never delays renewal.

## Context layout (what the model sees)

```
system     prompts/agent_system.md: role + team roster, task (goal, criteria,
           outputs with JSON schema), upstream inputs (data), artifacts, rules,
           screen size + coordinate-mode help. Built once per task; rebuilt only
           when the roster changes.
user       "Progress so far: <≤800 chars>" + "Earlier steps:" compact lines
           e.g. [step 12] click(x=512, y=300) → clicked left at (512, 300) → screen changed
┌ user     past step header (+ screenshot if within the image window, else "omitted")
│ assistant reasoning text + tool calls
└ tool …   one result per call                       × last 12 steps
user       "Step k of max B. Time used m min." + [system note] lines
           Inbox: <digest>   ·   Your notebook: <≤1500 chars>
           Current screen: <image>   (+ zoom image requested in the previous step)
```

- Images appear only in `user` messages. Only the last `max_images_in_context`
  observations are images, the current one included.
- Every 20 steps, steps older than the last 12 are folded into the summary by the
  `Summarizer`. Until then they appear as compact lines.
- The prompt budget is `prompt_budget_tokens` (24k for local models, 60k for
  cloud), estimated as 4 chars ≈ 1 token plus `image_tokens` per image. When over
  budget, the loop trims oldest images first, then oldest full steps (they become
  compact lines), then compact lines, then the digest. The current screenshot is
  never trimmed.

## Stuck detection

`StuckDetector` reports one `agent.stuck_detected` per episode, with the reason
and the last 3 frame ids. It then sets the status to `stuck`; the loop keeps
running and the Supervisor reacts. The episode closes, and the status returns to
`working`, once the screen changes after an action or progress evidence appears
(a successful `post`/`answer`/`message`/`release_claim`/`publish_artifact`, or a
`submit_result` attempt). Thresholds live in `StuckConfig`:

| Signal | Default |
|---|---|
| `screen_unchanged` | pHash distance ≤ 2 after 6 consecutive GUI actions (`run_command`, `wait`, `zoom` excluded) |
| `repetition` | same tool and args (±10 px) 4 times within 8 steps |
| `no_progress` | 40 steps without progress evidence |
| `error_streak` | 5 consecutive tool errors |
| `no_tool` | no tool call after 2 corrective retries |

Window-title/URL changes are not yet observable through `SandboxHandle`, so they
do not count as progress yet.
