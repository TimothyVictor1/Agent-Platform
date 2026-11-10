# FAQ

Short answers to questions that come up when using (or reading) the platform.
For deeper pages: [architecture](architecture.md) · [models](models.md) ·
[sandboxes](sandboxes.md) · [e2b provider](sandboxes-e2b.md) ·
[agent loop](agent-loop.md) · [coordination protocol](coordination-protocol.md) ·
[configuration](configuration.md). The spec (`SPEC.md`) is the source of truth.

## Running it

### Do I need a GPU?
Not necessarily. The three shipped model profiles cover the common options:

- `local-free`: local model servers (llama.cpp, Ollama, vLLM) — local and
  private, but it needs hardware that runs the models. Small vision
  models can run on CPU; GPUs make it comfortable.
- `openrouter-free`: free cloud models via OpenRouter — runs on any machine.
- `cloud-best`: paid Anthropic models.

The sandbox layer is independent of the model choice: local Docker desktops (free)
or E2B cloud VMs (free one-time credit on the Hobby tier).

### Why are there both Postgres and Redis?
Two different jobs. **Postgres** is the truth: every state change is an event
appended to the `events` table with a gap-free per-session `seq`, and read models are
updated in the same transaction — so services can restart and resume, and any
session can be replayed as one timeline. **Redis** carries the ephemeral traffic:
event fan-out, per-agent work queues with ack, heartbeats/thumbnails (never
persisted) and leases (TTL 45 s). The bus is best-effort; the database wins.

### Can I run several copies at once?
Yes — that is a supported setup. The compose project is fixed (`name:
agentplatform`), and each Superset worktree gets its own database (`APP_DB_NAME`),
API/frontend ports (`APP_API_PORT`, `PORT`) and worktree label (`APP_WORKTREE`) via
`scripts/superset-setup.sh`, so worktrees do not clash. Sandboxes get their own Docker
network, and cleanup only removes containers labelled with your worktree.

### Port 5432/6379 is already taken.
Split the settings: set `APP_PG_PORT`/`APP_REDIS_PORT` to a free host port for the
compose containers and point `DATABASE_URL`/`REDIS_URL` at that port. See
`docs/configuration.md`.

## Models

### Which models work?
Any server with an OpenAI-compatible chat API (llama.cpp, Ollama, vLLM, LM Studio,
OpenRouter, Gemini's OpenAI-compatible endpoint) via `provider: openai_compatible`,
or Anthropic via its native API. One hard requirement: the **agent** role must be a
vision model (`vision: true`) because every step starts from a screenshot.
Planner/supervisor/summarizer can be text models.

### Models that answer with plain text instead of tool calls?
The fallback parser turns text actions (`pyautogui.click(512, 300)`,
`{"tool": "click", "args": {...}}`, fenced JSON) into tool calls, so models without
reliable tool calling still drive the desktop. If the model returns no action at
all, the loop retries twice with a corrective message, then reports "stuck (no
tool)", which triggers the supervisor.

### Why 0..1000 coordinates, and when?
`coordinate_mode` per role: `absolute` (pixels in the screenshot — typical for
Anthropic), `normalized_1000` (typical for open GUI models) or `relative` (0–1). The
action layer converts everything into sandbox pixels, clamps to the screen and
handles downscaled observations, so the model can answer in whichever space it was
trained for. All three modes are unit-tested against resized screenshots.

### How do retries stack up?
Two layers multiply: the `RetryingClient` wraps every model call (up to 4 attempts on
429/5xx/timeouts, honouring `Retry-After`), and the agent loop retries whole steps
(fails the task after 3 consecutive model errors). A struggling server can see up to
12 requests for one step — deliberate, so a short outage does not fail tasks. See
`docs/models.md`.

## How agents work

### Where do agents actually run?
Outside the desktops. Each agent is an asyncio task in the runtime process; its
sandbox (a throwaway Linux desktop) only receives low-level actions — screenshots,
mouse/keyboard via the in-sandbox control API, shell commands, file moves. Model API
keys, agent code and other agents' data never enter a sandbox (SPEC §5.1, §9).

### How do agents talk to each other?
Through the board only. Every agent step ends with an **inbox digest** — a
token-budgeted, prioritized extract of new board entries (user steering > decisions >
answers to my questions > direct messages > facts/claims relevant to my tags …).
Direct messages are board entries with `to: [agent]` and are invisible to everyone
else. There is also a small scratchpad (`notebook_write`). The digest builder and its
worked example are in `docs/coordination-protocol.md`.

### How do files pass between agents?
The artifact store. `publish_artifact(path)` reads a file out of the sending sandbox,
stores it content-addressed (sha256, ≤ 50 MB) and announces it on the board;
`fetch_artifact(id)` writes it into `/home/agent/Shared/` of a receiving sandbox.
Artifacts of a task's upstream dependencies are pre-fetched into `Shared/` before the
task's first step, so downstream agents start with the files already there.

### What happens when an agent gets stuck?
`StuckDetector` watches five signals (screen unchanged by pHash, repeated
action ±10 px, no progress for 40 steps, error streak, no tool call). On detection
the agent gets status `stuck`, and the supervisor is triggered with the
last frames; the loop keeps running (the supervisor can pause/rework/replan). The
supervisor itself is task T17 and is not merged yet, so today the trigger event and
stuck status exist; automated rework/replan lands with T17.

### What happens if the runtime crashes mid-session?
Nothing is lost. State is event-sourced in Postgres; task assignments carry leases
(renewed by 10 s heartbeats, TTL 45 s). A fresh runtime re-adopts known agents,
reconnects (or re-creates) their sandboxes and continues; orphan assignments resolve
via ack timeout or lease expiry and the task is requeued with `attempt + 1`. The
restart end-to-end path is wired by T13 (not merged yet); the scheduler logic and its
tests are merged.

## Safety and control

### Could an agent read or type my passwords?
No. Receiving or entering passwords, card numbers or government IDs is in the
`forbidden` policies (violations come back as `ERROR: ...` to the model), agents must
never log into your real accounts (SPEC §2.2), and no secret from `.env` ever enters
a sandbox. Deliberate submit/purchase/send/delete/account actions require an approval
card you decide, with a screenshot attached (expiry after 15 min).

### Can I watch and intervene?
Always. Each desktop has a **view-only** noVNC stream in the UI (interactive only
during explicit takeover, with a prominent banner and "Return control"). You can
steer any agent with a message that lands in its next step at highest priority,
pause/resume, or hit the kill switch (all loops stop within 5 s, sandboxes killed).

### Is board/web content dangerous for the main system?
It is treated as data, never instructions. Board text is rendered as plain text
(never HTML), never executed, never used to build host commands; digest and
`read_board` lines are sanitized to one line each; only the task, supervisor/user
decisions and user steering are instructions in the prompt. The eval suite includes
a frozen-injection fixture page to prove agents ignore instructions found on web
pages (`PWNED` must never be typed — checked from events).

### Is anything sent to the cloud?
Only what your model profile points at. With `local-free`, conversation, screenshots
and coordinates stay on your machine (the agents' web browsing happens inside their
sandboxes, exactly as a user's browser would). With OpenRouter/Anthropic/Gemini
profiles, prompts and images go to that provider. E2B sandboxes run in E2B's cloud
regardless of the model choice.

## Troubleshooting

### My agent seems to do the same click twice / stalls ~20 s
Known `docker_local` issue (control server's `xdotool mousemove --sync` waits when
the pointer is already at the target). It affects `move()` and repeated clicks at the
same spot; it is recorded as an expected failure in the contract suite and the fix
belongs in `sandbox-image/` (`docs/sandboxes.md` → Known issues).

### The e2b sandbox died after an hour
That is the Hobby tier's maximum sandbox lifetime, surfacing as
`SandboxExpiredError` per call; `time_left()` warns 5 minutes ahead. Kill sandboxes
you don't need — billing is per second. `docs/sandboxes-e2b.md`.

### Config file changed but nothing changed
Take these paths: `.env` is read at process start (API/runtime); `NEXT_PUBLIC_*` is
inlined by Next.js at build/dev start (restart the dev server); `models.yaml` /
`policies.yaml` are loaded and validated at startup with readable errors. Also check
you edited the real file (`config/*.yaml`, git-ignored) and not just the `.example`.

### How are the evals run? Do I need real websites?
No. `evals/fixtures-site/` ships a frozen nginx website (four fake note-app vendors
with pricing pages, a task/doc site, a form, two sites that disagree on a fact, one
injection trap) so runs are deterministic and cheap. Tasks, checks, the runner CLI
and metrics are described in the SPEC §20 and `evals/README.md`; reports land in
`evals/reports/`. Running an actual platform evaluation requires the API/runtime
wiring (T09/T13).
