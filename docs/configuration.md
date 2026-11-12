# Configuration

The platform is configured from three sources, all loaded by
`backend/src/agentplatform/config.py`. `SPEC.md` §22, §8.2 and §15.1 are the source
of truth for what must exist; this page documents what each knob does and its default.

| Source | What it holds | How it is written |
|---|---|---|
| `.env` (repo root) | Secrets and process settings | Copy `.env.example` to `.env`; never commit it |
| `config/models.yaml` | Model profiles and price table | Copy `config/models.example.yaml`; never commit it (git-ignored) |
| `config/policies.yaml` | Approval, network, frame and budget policies | Copy `config/policies.example.yaml`; never commit it |

`Settings` (pydantic-settings) reads the environment and `.env` from the repository
root (the nearest ancestor directory containing `SPEC.md`). Secret values are stored
as `SecretStr`, so they never appear in reprs or logs. Unknown variables are ignored.

Notes that keep `.env` files from breaking things:

- Put comments **on their own line above** the variable, never after a value: `make`
  includes `.env` raw (comments would become part of the value) and dotenv parsers
  disagree about inline comments (SPEC §22).
- The frontend reads the **same repo-root `.env`**: `frontend/next.config.ts` loads it
  with `@next/env` so `NEXT_PUBLIC_*` values are shared with the backend. A
  `frontend/.env*` file overrides the root values.
- `scripts/superset-setup.sh` (Superset worktrees) copies the main `.env` and appends a
  per-worktree block: `APP_API_PORT` (8100 + offset), `PORT` (3100 + offset), matching
  `FRONTEND_ORIGIN` / `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_WS_URL`, a dedicated
  `APP_DB_NAME=agentplatform_<workspace>` and `APP_WORKTREE`.

## Environment variables (`.env`)

Every variable in `.env.example`, with its default (the value shipped there, or what
the code falls back to when it is empty) and who reads it.

### Core services

| Variable | .env.example / fallback | What it does | Read by |
|---|---|---|---|
| `DATABASE_URL` | *(empty)* → `postgresql+asyncpg://agentplatform:agentplatform@localhost:5432/agentplatform` | SQLAlchemy async connection string for Postgres (the event store, read models and repositories). `APP_DB_NAME` replaces the database name in this URL when set. | `db/session.py` (engine + sessionmaker) |
| `REDIS_URL` | *(empty)* → `redis://localhost:6379/0` | Redis connection for the event bus (Streams), work queues with ack, ephemeral messages (heartbeats, thumbnails) and leases. | `bus/redis_bus.py`, `bus/leases.py` |
| `APP_API_TOKEN` | *(empty)* → no auth (local development) | Optional static bearer token. When set: REST requires `Authorization: Bearer`, the WebSocket needs a single-use 60 s ticket (`POST /sessions/{id}/ws-ticket`); when unset both accept anonymous access. Behavior defined in SPEC §16. | API service (T09 — not merged yet) |

### Config file paths

| Variable | Default | What it does |
|---|---|---|
| `APP_MODELS_FILE` | `config/models.yaml` | Path to the model-profiles YAML, resolved relative to the repository root (absolute paths allowed). Loaded and validated once at startup. |
| `APP_POLICIES_FILE` | `config/policies.yaml` | Path to the policies YAML (same rules). |

### Artifact storage

| Variable | Default | What it does |
|---|---|---|
| `ARTIFACT_STORE` | `fs` | Which artifact store to build: `fs` (content-addressed filesystem, atomic writes) or `s3` (S3-compatible API). Session artifacts **and** replay frames both go through this store. |
| `ARTIFACT_DIR` | `./data/artifacts` | Root directory of the `fs` store (resolved relative to the repo root). `data/` is git-ignored. |
| `S3_ENDPOINT` | *(empty)* | Endpoint URL for the `s3` store (MinIO from the optional compose profile `s3` on `http://127.0.0.1:9000`, or Cloudflare R2). Empty means AWS default resolution. |
| `S3_BUCKET` | *(empty)* | Bucket name for the `s3` store. |
| `S3_ACCESS_KEY_ID` | *(empty)* | Access key (a secret: `SecretStr`, never logged). |
| `S3_SECRET_ACCESS_KEY` | *(empty)* | Secret key (idem). |

The optional compose MinIO is started with `docker compose --profile s3 up -d minio`
(ports 9000/9001 on 127.0.0.1, dev-only credentials in `docker-compose.yml`).

### Model credentials

Keys live **only here** — `models.yaml` never contains them; it names the variable via
`api_key_env`, and the client factory raises a `ConfigError` if that variable is
missing or empty while the profile uses it. Nothing secret is ever sent into a
sandbox.

| Variable | What it is for |
|---|---|
| `E2B_API_KEY` | The `e2b` cloud sandbox provider. Read from the backend environment only and passed per API call — never into a sandbox. Also gates the live e2b contract tests (needs `APP_LIVE=1` too). Missing key ⇒ `health()` reports not-ok (`E2B_API_KEY is not set`). |
| `ANTHROPIC_API_KEY` | Anthropic roles. This is also the default `api_key_env` for `provider: anthropic` when a profile omits it. |
| `OPENROUTER_API_KEY` | OpenRouter profiles (`base_url: https://openrouter.ai/api/v1`). |
| `GEMINI_API_KEY` | Gemini via its OpenAI-compatible endpoint (`generativelanguage.googleapis.com/.../openai/`). |
| `NEBIUS_API_KEY` | Nebius Token Factory profiles (`base_url: https://api.tokenfactory.nebius.com/v1`). |
| `LOCAL_LLM_KEY` | Only if your local model server was started with an API key (e.g. llama.cpp `--api-key`). Omit `api_key_env` in the profile when the server needs none; then no key is sent (`not-needed` placeholder keeps the SDK from leaking a real `OPENAI_API_KEY` to an unrelated server). |

### API, CORS and ports

| Variable | Default | What it does |
|---|---|---|
| `FRONTEND_ORIGIN` | `http://localhost:3000` | Browser origin the API serves CORS to. Rewritten per worktree by the Superset setup script. |
| `APP_API_PORT` | `8000` | Port the API process listens on. Smoke/run wiring lands with the API service (T09); keep it in sync with `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_WS_URL`. |
| `PORT` | `3000` | Port of the Next.js dev server (`make web` runs `pnpm dev --port $(PORT)`; Playwright also reads it). |

### Database and infrastructure ports

Ports are configurable so several Superset worktrees can run side by side against one
shared `docker compose` project (`name: agentplatform`).

| Variable | Default | What it does |
|---|---|---|
| `APP_DB_NAME` | `agentplatform` in `.env.example` (unset ⇒ no URL rewrite; `make` falls back to `agentplatform`) | Database name used in the effective `DATABASE_URL`; `make db-up` creates it in the compose Postgres if missing. Each worktree gets its own (`agentplatform_<name>`). |
| `APP_PG_PORT` | `5432` | Host port of the compose Postgres (published on 127.0.0.1). Must not clash with a local Postgres (e.g. Homebrew); if it does, pick a free port **and** point `DATABASE_URL` at it. |
| `APP_REDIS_PORT` | `6379` | Host port of the compose Redis (idem; point `REDIS_URL` at it if changed). |
| `APP_WORKTREE` | *(empty)* → repo folder name | Worktree label written onto sandbox containers (`agentplatform.worktree`) so startup cleanup and `scripts/superset-teardown.sh` remove only this worktree's containers. |

### Frontend (`NEXT_PUBLIC_*`)

Inlined by Next.js at build/dev start, so restart the dev server after changing them.

| Variable | Default | What it does |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | REST base URL used by the browser (`lib/api.ts`). |
| `NEXT_PUBLIC_WS_URL` | `ws://localhost:8000` | WebSocket base URL (`lib/ws.ts`). |
| `NEXT_PUBLIC_API_TOKEN` | *(empty)* | Bearer token the browser sends. Set it **only together with** `APP_API_TOKEN` (the token never goes into a URL; WebSocket auth uses tickets). |
| `NEXT_PUBLIC_MOCK` | `0` | `1` runs the UI purely on fixtures from `frontend/src/mocks/` (scripted session, no backend needed) — for UI development and demos without a model. |
| `NEXT_PUBLIC_APP_NAME` | `Agent Platform` | App title in the top navigation. |

### Sandbox egress (`docker_local`)

| Variable | Default | What it does |
|---|---|---|
| `APP_SANDBOX_EGRESS` | `open` | `open`: sandboxes have normal network access (and on Docker Desktop can reach host services). `proxy`: each sandbox has no default route and reaches only the compose `egress-proxy`, which refuses non-public destinations. Start the proxy first with `docker compose --profile egress up -d egress-proxy`; without it, sandbox creation fails. See `docs/security.md`. |
| `APP_EGRESS_PROXY_CONTAINER` | `agentplatform-egress-proxy-1` | Name of the proxy container the provider attaches to each sandbox's internal network. |
| `APP_EGRESS_ALLOW_EVAL_FIXTURES` | `0` | Read by the compose proxy service. `1` lets exactly the host name `eval-fixtures` (the eval fixture site) resolve to a private address, never a loopback, link-local or unspecified one. Every other private name or IP is always refused. Turn it on only for eval runs (recreate the proxy after changing it). |

### Test flags

| Variable | Default | What it does |
|---|---|---|
| `APP_LIVE` | `0` | Enables live tests (`backend/tests/live/`) and the `e2b` parameter of the sandbox contract suite (also needs `E2B_API_KEY`). Live tests use real models and real credit; they are skipped otherwise. |
| `APP_DOCKER` | `0` | Enables the Docker-dependent parameters of the sandbox contract suite (`docker_local`). |

Anything else in the environment is ignored by `Settings`.

## `config/models.yaml` (model profiles)

Maps the four roles — `planner`, `supervisor`, `agent`, `summarizer` — to concrete
models and endpoints. All four roles are **required** in every profile, and the code
validates the file strictly (unknown keys are errors, per-role checks below).

```yaml
default_profile: local-free   # used when a session does not name a profile; must exist

profiles:
  <name>:
    planner:    { ... }       # turns the request into a task DAG (text)
    supervisor: { ... }       # coordination, review, re-planning (text)
    agent:      { ... }       # the desktop worker — MUST be a vision model
    summarizer: { ... }       # folds old history into progress notes (small model)

prices:                       # USD, used for Usage.cost_usd (missing model id = all 0)
  "<model id>": { input: 3.0, output: 15.0, image: 0.0 }
```

### Role fields

| Key | Default | Meaning |
|---|---|---|
| `provider` | *(required)* | `openai_compatible` or `anthropic`. |
| `model` | *(required)* | Model id sent to the server. |
| `base_url` | *(required for `openai_compatible`)* | Server URL, e.g. `http://host:8080/v1`. A non-HTTPS URL counts as "local" (see prompt budget below). |
| `api_key_env` | none (`ANTHROPIC_API_KEY` for `provider: anthropic`) | Name of the `.env` variable holding the key — never the key itself. Omit for keyless local servers. |
| `vision` | `false` | Whether images are sent to this role. The `agent` role must set `vision: true`. |
| `coordinate_mode` | `absolute` | How the model expresses screen coordinates: `absolute` (pixels in the screenshot it saw), `normalized_1000` (0–1000 on both axes), `relative` (0–1 floats). The action layer converts to sandbox pixels; only valid on vision roles. |
| `screenshot_target_width` | `1280` (320–3840) | Width observations are resized down to (never upscaled). |
| `max_images_in_context` | `3` (1–10) | How many recent screenshots stay visible in the prompt (older ones become text). |
| `concurrency` | `4` (1–64) | Semaphore size in front of the server for this role — keep ≤ what the server can handle in parallel (e.g. llama.cpp `--parallel`, Ollama `OLLAMA_NUM_PARALLEL`). |
| `max_output_tokens` | `1024` (≥ 16) | Per-request output cap. Anthropic models think by default and thinking tokens count against this — give them 4096–16000 (see `docs/models.md`). |
| `temperature` | `0.0` (0–2) | Sampling temperature. *Not sent to Anthropic (the Messages API rejects it).* |
| `timeout_s` | `120.0` (> 0) | Whole-call bound for one model request, retries included. |
| `prompt_budget_tokens` | *(empty)* → `24 000` local, `60 000` cloud | Total prompt budget per step. "Local" = a non-HTTPS `base_url`. |
| `image_tokens` | `1500` | Approximate token cost of one screenshot, used by the context budgeter. |

Three example profiles ship in `config/models.example.yaml`: `local-free` (local
llama.cpp-style servers), `cloud-best` (Anthropic) and `openrouter-free`. Concrete
recipes for llama.cpp, Ollama, vLLM, OpenRouter, Gemini and Anthropic are in
`docs/models.md`. `GET /config/providers/health` (once the API service is merged)
pings every configured endpoint via `client.health()`.

### Prices

`prices` is a flat table keyed by model id: `input` and `output` per 1M tokens and
`image` per image, all in USD. A model with no entry costs 0 (that is why local and
`:free` models need no row). The accounting layer fills `Usage.cost_usd` from it and
the UI meters cost against the session budget.

## `config/policies.yaml` (safety policies)

Defaults below are what the loader uses when a key is absent (from
`PoliciesConfig` in `config.py`); the shipped example states them explicitly.

| Key | Default | Meaning |
|---|---|---|
| `approval_required_for` | *(empty list)* | Categories of actions an agent must never perform without calling `request_approval` (submitting third-party forms, purchases, sending messages, deleting files outside `/home/agent/Shared` or `/tmp`, creating accounts/logging in). Free-text lines; injected into the agent system prompt. |
| `forbidden` | *(empty list)* | Categories agents must never do at all (entering passwords/card numbers/government IDs, changing sandbox security settings). Violations surface to the model as `ERROR` tool results. |
| `network.mode` | `open` | Sandbox egress mode: `open`, or `allowlist` (its rules are passed to the provider; e2b supports it, `docker_local` currently refuses it — see `docs/sandboxes.md`). |
| `network.allowlist` | *(empty list)* | Entries for `mode: allowlist` (IPs, CIDR blocks or hostnames). |
| `frames.keep_full_resolution` | `false` | When `false` only the 480 px JPEG frames used for live thumbnails and replay are kept. |
| `budgets.default_max_cost_usd` | `5.0` (`null` allowed) | Default cost cap pre-filled on new sessions. Sessions can set their own budgets (per-task steps 150, total steps 1500, wall minutes 60 are the session-level defaults in SPEC §6.1). |
| `require_human_for_plan_changes` | `false` | When `true`, supervisor plan edits (add/update/cancel task) are routed to the approval queue instead of applying directly (SPEC §14.3). |
| `auto_approve_followups` | `false` | When `false`, a follow-up (`POST /sessions/{id}/followup`) needs plan approval again before execution. |

**Enforcement status (honest, since parts of the runtime are still pending).** The
loader parses and validates this file today (`ConfigError` with readable errors).
Its actual enforcement points are wired by tasks still pending on `main`: the
scheduler/runtime wiring (T13) feeds `network` into sandbox specs and `frames` into
the agent loop, and the human-in-the-loop backend (T18) enforces approvals,
forbidden typing, `require_human_for_plan_changes` and the default cost cap.
Until those merge, editing `policies.yaml` changes nothing at runtime — it only
changes what the config would request. The coordination-layer rules that the
board/digest task (T14) already enforces (entry-kind rules, direct-message
visibility, one-line sanitization) do not depend on this file.

## Scheduler/runtime knobs (not YAML)

Operational knobs of the scheduler and agent pool are code defaults in
`SchedulerConfig` / `PoolConfig` / `AgentRuntimeConfig` (ack timeout 10 s, lease TTL
45 s, boot concurrency 3, idle timeout 300 s, review mode `auto` until the
supervisor lands, 1280×800 screens, stuck-detector thresholds, prompt
budgets). They are documented with their defaults in
`docs/coordination-protocol.md` (Scheduling / Configuration) and
`docs/agent-loop.md` (Stuck detection / Context layout).
