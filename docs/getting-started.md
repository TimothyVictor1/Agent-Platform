# Getting started

This page takes you from a fresh clone to **first light**: one real agent finishes one
real task on a local desktop. Run every command from the repository root.

## 1. Prerequisites

- Docker (Desktop or Engine), running.
- [`uv`](https://docs.astral.sh/uv/) and Python 3.12.
- Node.js with `pnpm` (or `corepack`, which the Makefile uses when `pnpm` is missing).

## 2. One-time setup

```bash
cp .env.example .env            # Superset worktrees already have one (scripts/superset-setup.sh)
cp config/models.example.yaml config/models.yaml
cp config/policies.example.yaml config/policies.yaml
make install                    # backend (uv) and frontend (pnpm) dependencies
make sandbox-image              # builds agentplatform/desktop:latest (a few minutes)
make db-up                      # Postgres + Redis (compose) and this worktree's database
make db-migrate                 # Alembic migrations
```

Ports and names come from `.env`:

| Variable | Default | What |
|---|---|---|
| `APP_API_PORT` / `APP_API_HOST` | `8000` / `127.0.0.1` | API |
| `PORT` | `3000` | web app |
| `APP_PG_PORT`, `APP_REDIS_PORT` | `5432`, `6379` | compose host ports |
| `APP_DB_NAME` | `agentplatform` | this worktree's database |

If Postgres is not on 5432 (for example the shared compose Postgres on another port),
set `DATABASE_URL` explicitly, e.g.
`DATABASE_URL=postgresql+asyncpg://agentplatform:agentplatform@localhost:5434/agentplatform`.

The compose credentials are for local development only. A real deployment sets
`APP_PG_PASSWORD` and `APP_REDIS_PASSWORD` (or full `DATABASE_URL`/`REDIS_URL`) to
strong values. The Postgres password only applies when its data volume is created.

## 3. Choose a model profile

Edit `config/models.yaml`. A profile maps the four roles (planner, supervisor, agent,
summarizer) to models; the agent role needs a vision model. API keys never go into
the YAML: `api_key_env` names the variable, and the value goes into `.env`:

| Profile style | Put in `.env` |
|---|---|
| Anthropic (`provider: anthropic`) | `ANTHROPIC_API_KEY=...` |
| OpenRouter | `OPENROUTER_API_KEY=...` |
| Gemini (OpenAI-compatible endpoint) | `GEMINI_API_KEY=...` |
| Nebius Token Factory | `NEBIUS_API_KEY=...` |
| Local server (llama.cpp, Ollama, vLLM) | `LOCAL_LLM_KEY=` (often empty) |

Replace the `<...>` placeholders with real model names. Set `default_profile` to the
profile the web app should preselect.

## 4. Run everything

```bash
make dev
```

`make dev` starts the databases (`db-up`), applies migrations, then runs the API, the
runtime and the web app with prefixed logs (`[api]`, `[runtime]`, `[web]`). Ctrl-C
stops all three. Open `http://localhost:$PORT` (default 3000).

The runtime logs `runtime_started` once it has recovered earlier sessions. It
refuses to start (exit code 2, `runtime_refused_to_start`) when a configured secret
would reach a sandbox, and it warns when `.env` is tracked by git.

## 5. First light

The live test runs the API and the runtime in one process against a throwaway
database (created and dropped by the test) on your Postgres, your Redis, the local
desktop image and the model profile you name. It never touches your own sessions or
desktops.

```bash
make db-up                      # if the databases are not running yet
cd backend
APP_LIVE=1 APP_DOCKER=1 APP_LIVE_PROFILE=<your profile> \
  uv run pytest tests/live/test_first_light.py -s
```

Stop `make dev` in the same worktree first: its runtime's orphan cleanup removes this
worktree's desktops of sessions it does not know, and the test's session lives in the
throwaway database.

It creates the session *"Open the browser, go to example.com and report the page
title (the text shown in the browser tab or window title) as output `title`"*,
approves the plan, waits for completion (up to 20 minutes) and checks:

- the reported `title` is `Example Domain` (trimmed, case-insensitive; example.com
  has no `<h1>` any more, but its `<title>` is still this);
- frames were stored and can be downloaded;
- the session's event seqs are contiguous (1, 2, 3, ...).

**Cost.** With a cloud profile this calls paid APIs (planner, agent, supervisor,
summarizer). The session budget is capped at `APP_LIVE_MAX_COST_USD` (default 2 USD);
the test prints the steps and the cost at the end.

## 6. Everyday commands

```bash
make test                       # unit + integration tests with fakes (no network, no Docker)
cd backend && uv run pytest -m infra   # tests that need Postgres + Redis (make db-up)
make lint                       # ruff, format check, mypy --strict, frontend lint/typecheck
make contracts                  # regenerate JSON Schema + TypeScript contracts
```

## Troubleshooting

- **A session stays in "planning".** The runtime plans sessions whose plan command
  never arrived after `APP_PLANNING_SWEEP_S` (60 s). Check the runtime log for
  `planning_failed` (the planner model is unreachable or answered invalid JSON).
- **Desktops pile up.** The runtime removes this worktree's desktops of ended
  sessions at startup and every `APP_ORPHAN_SWEEP_MIN` minutes. `docker ps --filter
  label=agentplatform.provider=docker_local` lists them.
- **Several worktrees.** They share the compose Postgres and Redis. Each worktree
  uses its own database (`APP_DB_NAME`) and its own ports; a runtime never runs a
  session that is not in its database.
- **After pulling T13, `make db-up` restarts Redis once** (its compose command
  changed to support `APP_REDIS_PASSWORD`). Its data volume is kept.
