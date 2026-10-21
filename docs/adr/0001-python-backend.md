# ADR 0001: Python-only backend and runtime

Status: accepted (T00)

## Context

Earlier systems of this kind split logic between a TypeScript backend and Python
agent code, and lost time and correctness on the glue across process boundaries.
The agent loop, model SDKs, image processing (Pillow, imagehash) and sandbox SDKs
(Docker, E2B) are all first-class in Python.

## Decision

The API (FastAPI) and the runtime (asyncio) are Python 3.12 in one package,
`agentplatform`, run as two processes. The Next.js frontend is a pure client of the
API with no business logic in API routes or a custom server. Tooling: `uv`, `ruff`,
`mypy --strict`, `pytest`.

## Consequences

- One language for every rule that matters; contracts are defined once in Pydantic.
- The frontend gets types by generation (ADR 0002 and SPEC §5.1 decision 6), never
  by hand.
- Agents are asyncio tasks, so all I/O must be async; sync SDKs are wrapped with
  `asyncio.to_thread`.
