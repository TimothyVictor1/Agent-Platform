# Architecture

This page summarises how the platform is put together and records decisions taken
while building it. `SPEC.md` remains the source of truth; when this page and the
spec disagree, the spec wins and this page gets fixed.

## Components

```
┌──────────────────────── Browser (Next.js app) ────────────────────────┐
│ Home · Plan Review (DAG editor) · Live Session · Replay · Settings     │
└───────────────▲───────────────────────────────▲───────────────────────┘
                │ REST (JSON)                   │ WebSocket (events, resumable by seq)
┌───────────────┴───────────────────────────────┴───────────────────────┐
│                   API service (FastAPI, Python 3.12)                   │
│  sessions · plans · tasks · board · artifacts · approvals · ws gateway │
└───────────────▲───────────────────────────────────────────────────────┘
                │ Event bus (Redis Streams) + Postgres (event store, read models)
┌───────────────┴───────────────────────────────────────────────────────┐
│                 Runtime service (Python asyncio, same repo)            │
│  Planner · Scheduler · Supervisor · AgentRuntime ×N · StuckDetector    │
│  ModelClient (provider-agnostic) · SandboxProvider (docker | e2b)      │
└───────▲───────────────────────────▲───────────────────────────────────┘
        │ control API / SDK         │ OpenAI-compatible or native APIs
┌───────┴─────────┐        ┌────────┴──────────────────────────────────┐
│ Sandboxes       │        │ Model servers                             │
│ docker-local    │        │ llama.cpp / Ollama / vLLM (local, free)    │
│ e2b (cloud)     │        │ OpenRouter · Gemini · Anthropic (cloud)    │
└─────────────────┘        └───────────────────────────────────────────┘
        ▲
        │ files in/out
┌───────┴─────────┐
│ Artifact store  │  filesystem (default) or S3-compatible (MinIO/R2)
└─────────────────┘
```

## Where things live

| Concern | Module | Notes |
|---|---|---|
| Contracts (domain, events, commands, API DTOs) | `backend/src/agentplatform/contracts/` | Source of truth; exported to `frontend/src/contracts/generated.ts` |
| DAG validation | `contracts/plan.py` (`validate_plan`, `topological_order`) | Pure functions, used by API, planner, UI mirror |
| Model interface | `models/base.py` (`ModelClient`) | Providers in `models/` (T02) |
| Sandbox interface | `sandboxes/base.py` (`SandboxProvider`, `SandboxHandle`) | Providers in `sandboxes/` (T07, T08); E2B: [sandboxes-e2b.md](sandboxes-e2b.md) |
| Event store | `db/event_store.py` (`EventStore`) | Postgres implementation (T04) |
| Bus + leases | `bus/base.py` (`EventBus`, `LeaseStore`) | Redis implementation (T04) |
| Emitting events | `eventing.py` (`EventRecorder`) | append → publish, the only way to emit |
| Blob storage | `artifacts/base.py` (`ArtifactStore`) | fs/S3 (T15) |
| Board | `runtime/coordination/board.py` (`BoardService`) | Implementation T14 |
| Tools | `runtime/agent/tools/registry.py` (`ToolRegistry`) | Tool groups filled by T06, T14, T15 |
| Action layer | `runtime/agent/actions.py` | T03 completes coordinate modes |
| Agent step loop | `runtime/agent/loop.py` (`AgentRuntime`) | See [agent-loop.md](agent-loop.md) |
| Config | `config.py` | env (`Settings`) + `models.yaml` + `policies.yaml` |
| Fakes | `testing/` | `FakeModel`, `FakeSandbox`, `InMemoryBus`, `InMemoryEventStore`, … |

## Flow of a user action

1. The UI calls the API (e.g. `POST /sessions/{id}/approve`).
2. The API validates against current read models and enqueues a typed `Command`
   (`contracts/commands.py`) on the session's command queue, answering
   `202 {command_id}`. The API never runs agents.
3. The runtime that owns the session (DB lease on `sessions.runtime_owner`) dequeues
   the command, does the work, and emits events through `EventRecorder`:
   `EventStore.append` (event + read models in one transaction) then
   `EventBus.publish`.
4. The WebSocket gateway streams events to the UI; the UI reducer
   (`applyEvent`) rebuilds live state. On reconnect the client sends
   `since=<last seq>` and the gateway replays from Postgres before going live.

## Decisions

From SPEC §5.1 (do not change without updating the spec):

1. Backend and runtime are Python only; the frontend is a pure API client.
2. API and runtime are two processes communicating only through the bus and the DB.
3. Agents are asyncio tasks inside the runtime, not OS processes.
4. Agents run outside sandboxes; sandboxes only receive low-level actions.
5. Event sourcing in Postgres with a per-session gap-free `seq`.
6. Contracts are generated, not hand-copied.

Decisions taken in T00 (implementation details the spec leaves open):

| # | Decision | Why |
|---|---|---|
| D1 | `Event` is a discriminated union of one envelope class per event type (`type` literal + typed `payload`). Producers build `EventDraft(payload, actor)`; stores call `build_event`. | Typed payloads in Python and narrowing on `event.type` in TypeScript. |
| D2 | `agent.heartbeat` and thumbnails are not `Event`s but ephemeral bus messages (`Heartbeat`, `Thumbnail`). | They are never persisted, so they have no `seq`. |
| D3 | Added event `session.planning_failed` (planner failed after its repair call). | SPEC §11.3 requires a clear error in the UI; no listed event carried it. |
| D4 | `BoardEntry.seq` equals the `seq` of its `board.entry_posted` event. `EventStore.append` accepts a draft factory `seq -> EventDraft` to make this possible. | One ordering for board entries and events; the digest cursor is an event seq, so it can never skip an entry. |
| D5 | API→runtime commands are contracts (`contracts/commands.py`), carried on work queues: `sessions:new` for new sessions, `session:{id}:commands` per session, `agent:{id}` for task assignments. | Avoids a later contracts change; one consumer per session. |
| D6 | Work queues are at-least-once with ack + visibility timeout; consumers de-duplicate by event/command id. The session stream is best-effort fan-out, DB is truth. | Restarts never lose state; subscribers recover from the store. |
| D7 | `LeaseStore` interface (acquire/renew/release/expired/forget) lives in `bus/base.py`. | T11 needs task leases and in-memory tests before T04's Redis version is merged. |
| D8 | `ToolRegistry` is generic over the handler context; tools return `ToolResult(text, is_error, outcome, image)`; mode gating via `for_mode`, phase gating via `hidden`. | The loop (T06) owns its context type without editing the registry. |
| D9 | `ModelClient` and `SandboxProvider` have `health()` returning `HealthStatus`; `ModelClient.aclose()`. | Needed by `GET /config/providers/health` (T09). |
| D10 | Contract additions beyond SPEC §6: `Task.rework_count`, `Artifact.description`, `Approval.expires_at`, `TaskResult` outputs typed as JSON values, `SessionDetail.last_seq`. | Required by review (§14.4), `publish_artifact(description)`, approval expiry (§15.2) and WS resume. |
| D11 | One shared `docker compose` project (`name: agentplatform`) for all worktrees; each worktree gets its own database via `APP_DB_NAME`. | Several Superset worktrees can run side by side without port clashes. |
| D12 | JSON Schema export uses serialization mode with defaults required; `*Request` models only require fields without defaults. | Response types have no needless `?`; requests can omit optional fields. |
| D13 | Contracts patch T00.x: WebSocket auth by single-use 60 s ticket (`POST /sessions/{id}/ws-ticket` → `WsTicket`, connect with `?ticket=`, close code 4401 = fetch a new ticket); `Message.thinking_blocks` (assistant only, echoed back unchanged); `validate_plan(max_tasks=12)`; planner codes in `PlanErrorCode`; `causation_id` = originating `command_id` on `session.plan_proposed`/`plan_updated`; `APP_REDIS_PORT`. | The API token never goes in a URL; providers need thinking blocks echoed; `extend` may add 12 new tasks; one error-code list for UI and API; the UI can match a stored plan to its request; worktrees can avoid port clashes. |

## ADRs

- [0001 Python backend](adr/0001-python-backend.md)
- [0002 Event sourcing](adr/0002-event-sourcing.md)
- [0003 Provider-agnostic models and sandboxes](adr/0003-provider-agnostic-models-and-sandboxes.md)

## Planner

`runtime/planner.py` (SPEC §11) turns a request into a validated task DAG through the planner role's `ModelClient`. Prompts live in `runtime/prompts/planner_*.md`: a static system prompt with every §11.2 rule and two few-shot plans (unit tests check that both examples pass `validate_plan`), plus `string.Template` user messages for propose, refine, extend and repair. User text and plan JSON are put in delimited data blocks.

- **One call:** request JSON (`response_format="json"`) → `extract_json_object` (code fences first, then the first balanced `{…}` that respects strings and escapes) → `PlanProposal` validation → `validate_plan` plus the mode's own checks → on any problem, exactly **one** repair call listing the numbered errors → otherwise `PlannerError` whose `errors` have the `PlanError` shape. Model errors propagate unchanged.
- **Keys:** `propose` renames tasks to `T1..Tn` in topological order. `refine` keeps the keys of tasks that still exist and numbers new tasks after the highest `T<n>`, so `diff_plans` (added/removed/changed by key, field-level before/after) stays meaningful. `extend` asks the model for new tasks only, copies the existing tasks from the input unchanged and appends the new tasks with the next free keys.
- **Limits:** the agent count goes to the model as a parallelism hint only; no code pads or splits tasks. 12 tasks per plan, including after follow-ups (`EXTEND_MAX_TOTAL_TASKS`, to be relaxed once `validate_plan` takes a limit). More than 3 `questions_for_user` are trimmed, and `suggested_tools` the agents do not have are dropped; both are logged.
- **Planner-only error codes:** `invalid_json`, `invalid_plan`, `invalid_task`, `key_collision`, `depends_on_unusable_task` (a follow-up depends on a failed/cancelled task), `no_new_tasks`.
