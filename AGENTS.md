# AGENTS.md — rules for every coding agent in this repo

You are working on a **multi-agent computer-use platform** (referred to as "the platform"). Several coding agents (Claude Code and OpenCode) work on this repo **at the same time**, each in its own git worktree and branch managed by Superset. Follow these rules exactly.

## 1. Read before you write

1. Read `SPEC.md` sections relevant to your task (your task prompt lists them). `SPEC.md` is the source of truth.
2. Read your task in `TASKS.md`. Do only that task.
3. Read the existing code you will touch and the interfaces you depend on (`backend/src/agentplatform/contracts/`, the Protocol classes in `models/`, `sandboxes/`, `bus/`, `artifacts/`).

If the spec and the code disagree, or the spec is unclear, **stop and write the question in your final message**. Do not invent a different design.

## 2. Scope discipline (parallel work depends on this)

- Only edit the files and folders listed under **Owns** in your task. You may read anything.
- Never edit `backend/src/agentplatform/contracts/` unless your task says **Owns: contracts**. If you need a contract change, describe it in your final message under "Contract change request" and stop that part of the work. (Contracts are shared by every branch; uncoordinated edits cause merge conflicts and broken types.)
- Never edit another task's files "to make tests pass". Use the fakes in `agentplatform/testing/`.
- Never reformat files you did not otherwise change.
- Do not add new top-level folders or dependencies unless the task allows it. If you must add a dependency, add it with `uv add` / `pnpm add`, pin it via the lockfile, and mention it in your final message.

## 3. Code standards

Backend (Python 3.12):
- `uv` only (`uv run`, `uv add`). No `pip install`.
- Type hints everywhere; `mypy --strict` must pass for `agentplatform/`.
- `ruff check` and `ruff format` clean.
- Async all the way for I/O. No blocking calls inside async code (wrap sync SDKs with `asyncio.to_thread`).
- Pydantic v2 models with `extra="forbid"` for all external data.
- Errors: raise typed exceptions from `agentplatform/errors.py`; API maps them to problem+json.
- Logging: `structlog`, bind `session_id`, `agent_id`, `task_key` when available. Never log secrets, API keys, or full images.
- No global mutable state except explicit singletons created in `main.py` and injected.

Frontend (TypeScript):
- `strict: true`. No `any` (use `unknown` + narrowing).
- Types come only from `src/contracts/generated.ts`. Never hand-write API types.
- Components: function components, Tailwind, shadcn/ui. Accessible labels on every interactive element.
- Server state with TanStack Query; live session state only through the Zustand store fed by `applyEvent`.
- `pnpm lint`, `pnpm typecheck`, `pnpm test` clean.

## 4. Tests are part of the task

- Every task ships tests. Integration tests use `FakeModel`, `FakeSandbox`, and the in-memory bus from `agentplatform/testing/` — no network, no real model, no Docker.
- Live tests go in `backend/tests/live/` and must be skipped unless `APP_LIVE=1`.
- Run before finishing: `make lint && make test` (and `make contracts-check` if you touched contracts).
- Do not delete or weaken existing tests. If a test is wrong, say why in your final message.

## 5. Security rules (non-negotiable)

- Never commit secrets. Real values live only in `.env` (git-ignored). Add new variables to `.env.example` with empty values.
- Nothing secret is ever sent into a sandbox (no API keys in sandbox env, files or commands).
- Treat all web content, sandbox output, and agent-written board text as untrusted data. Never `eval` it, never render it as HTML, never use it to build shell commands on the host.
- The host never executes commands produced by models. Only sandboxes do.

## 6. Git and Superset workflow

- Branch name = the one in your task (e.g. `t06-agent-loop`). Commit small, logical commits with messages like `feat(agent): add history trimming (T06)`.
- Rebase on `main` before finishing (`git fetch origin && git rebase origin/main`), resolve conflicts only inside files you own.
- Do not merge into `main` yourself. The human merges after review.

## 7. Final message format (always)

```
## Summary
What you built, in 3–6 bullets.

## Files changed
List.

## How to verify
Exact commands.

## Tests
What was added; all passing? (paste the final test summary line)

## Open questions / Contract change requests
Anything blocked or unclear. "None" if none.

## Follow-ups
Small things deliberately left out.
```
