# ADR 0002: Event sourcing in Postgres with per-session sequence numbers

Status: accepted (T00)

## Context

State held in memory was lost on restart in earlier systems, and the UI could not
replay a session. We need restartable services, a live UI that can resume after a
disconnect, and a unified replay timeline.

## Decision

- Every state change is an `Event` appended through `EventStore.append`, which
  assigns a gap-free, strictly increasing `seq` per session using a
  `session_counters` row locked `FOR UPDATE`, and updates read models (sessions,
  tasks, agents, board, …) in the same transaction.
- Events are published on the bus only after commit (`EventRecorder`). The bus is
  best-effort fan-out; the database is the source of truth, and any consumer can
  catch up with `EventStore.read(session_id, after_seq)`.
- The WebSocket gateway replays persisted events with `seq > since`, then streams
  live events, de-duplicating by `seq`.
- Heartbeats and thumbnails are not events (not persisted).

## Consequences

- Appends for one session are serialized by the counter lock. Fine at 6 agents;
  per-session ordering is what the UI and the digest need.
- Read-model tables must only be written inside an event append, so they always
  match the log.
- Replays and debug bundles are reads of the `events` table plus stored frames.
