# Sandboxes: providers and security notes

How the platform creates and drives agent desktops (SPEC §9). The interface lives in
`backend/src/agentplatform/sandboxes/base.py` (`SandboxProvider`, `SandboxHandle`).
Every provider passes the same contract suite,
`backend/tests/integration/test_sandbox_contract.py`.

| Provider | Code | Contract suite runs when |
| --- | --- | --- |
| `fake` (tests only) | `testing/fake_sandbox.py` | always |
| `docker_local` | `sandboxes/docker_local.py` (T07) | `APP_DOCKER=1` |
| `e2b` | `sandboxes/e2b_provider.py` (T08) | `APP_LIVE=1` and `E2B_API_KEY` set |

## `docker_local`

`DockerLocalProvider` runs one `agentplatform/desktop:latest` container per sandbox.
The image is built from `sandbox-image/` (`make sandbox-image`; see
`sandbox-image/README.md`). The provider then drives the in-container control
server (port 7000) over HTTP. The sandbox id is the container id.

### Running it

```sh
make sandbox-image                                  # once, or after image changes
cd backend && APP_DOCKER=1 uv run pytest tests/integration/test_sandbox_contract.py -q
```

`provider.health()` reports whether the Docker daemon is reachable and the image is
present. A missing image gives the message "run `make sandbox-image`".

### What `create(spec)` does

1. Validates the spec:
   - `network="allowlist"` raises `NotSupportedError`. Plain Docker cannot enforce
     an egress allowlist, and silently giving the sandbox open network access would
     be worse than failing.
   - `spec.env` must not set a variable the image reads itself (`CONTROL_TOKEN`,
     `CONTROL_TOKEN_FILE`, `SCREEN_*`, `DISPLAY`, `XAUTHORITY`, `XDG_RUNTIME_DIR`,
     `SANDBOX_FILE_ROOT`, `HOME`, `USER`, `PATH`).
2. Creates the bridge network `agentplatform-sandboxes` if it does not exist, with
   `com.docker.network.bridge.enable_icc=false`.
3. Generates a control token (`secrets.token_urlsafe(32)`) and creates the container
   with the flags below.
4. Stores the token in the `ControlTokenStore`, starts the container and reads the
   random host ports.
5. Waits up to 60 s for `/health` and a successful `/screenshot`. The desktop is
   ready when a screenshot works. On any failure, timeout or cancellation the
   container is force-removed, the token is deleted and `SandboxError` is raised.

### Container flags

| Setting | Value | Why |
| --- | --- | --- |
| Capabilities | `--cap-drop ALL --cap-add SETUID --cap-add SETGID` | The minimum T01 verified. Root (entrypoint and supervisord) only *drops* privileges to the `agent` user, which needs SETUID/SETGID. All file ownership is fixed at build time, so neither `CHOWN` nor `DAC_OVERRIDE` is needed. |
| `--security-opt` | `no-new-privileges` | No setuid binary can regain privileges. |
| Privileged | never | |
| Volumes / mounts / devices | none | A host mount defeats the isolation entirely. |
| Memory | `4g`, swap disabled (`memswap_limit=4g`) | SPEC §9.2 default. |
| CPU | 2 vCPU (`nano_cpus=2e9`) | SPEC §9.2 default. |
| PIDs | `pids_limit=4096` | Stops fork bombs. The limit counts threads, and Firefox plus LibreOffice stay far below it. |
| Ports | `127.0.0.1::7000`, `127.0.0.1::6080` | Loopback only, random host ports. |
| Network | `agentplatform-sandboxes`, ICC disabled | One sandbox cannot reach another's control or noVNC port. |

Labels (none secret) used for cleanup and debugging:

| Label | Value |
| --- | --- |
| `agentplatform.provider` | `docker_local` |
| `agentplatform.managed` | `1` |
| `agentplatform.worktree` | `APP_WORKTREE`, or the repo folder name. Sanitized like `scripts/superset-teardown.sh`, which removes the worktree's containers by this label. |
| `agentplatform.session`, `agentplatform.agent` | From the spec (may be empty). |
| `agentplatform.created_at`, `agentplatform.expires_at` | ISO timestamps; `expires_at` = `created_at` + `spec.timeout_minutes`. |

### Control token

- **How it reaches the container.** The token is passed as the `CONTROL_TOKEN` env
  var, exactly as `sandbox-image/README.md` describes. The entrypoint moves it into
  `/run/control-token` and unsets it, so no process environment inside the
  container carries it.
- **Where it is stored.** Never in the image and never in a label. `connect()` gets
  it back from the `ControlTokenStore` passed to the provider's constructor. The
  default `InMemoryControlTokenStore` only lives as long as the process; the runtime
  wiring has to pass a durable store, see "Open points".
- **Who can read it.**
  - Anyone who can talk to the Docker socket can see it with `docker inspect`
    (`Config.Env`). Docker socket access is root-equivalent anyway.
  - The `agent` user inside the container can read the token file (by design).
- **What it is.** The token only guards that one container's control API. It is not
  a user secret and must never grant access to anything else.
- **No proxy.** The HTTP client ignores `HTTP(S)_PROXY` (`trust_env=False`), so the
  bearer token never goes through a proxy.

### Handle behaviour

- **Retries.** Requests that change state (`click`, `type_text`, `key`, `scroll`,
  `drag`, `run_command`) are retried only on connect errors, when nothing was sent.
  A reset after sending is not retried, because a repeated click would click twice.
  Reads, `move` and `write_file` are also retried on read and protocol errors. There
  are at most 3 attempts.
- **Errors.**
  - Every failure raises `SandboxError`.
  - It is `SandboxExpiredError` once the container is gone or stopped. The handle
    checks the container's state when the control server is unreachable.
  - Error details from the control server are untrusted. They are stripped of
    control characters, capped at 500 characters and never contain the token.
- **Argument checks.** Arguments are checked locally before any request:
  coordinates inside `screen_size`, click count 1–3, scroll amount 1–50, text up to
  2000 characters, `interval_ms` 0–250, absolute paths, files up to 50 MB.
- **`run_command` truncation.** The control server cuts stdout and stderr at 8 KB
  but does not say whether it cut anything. Output that reaches 8 KB is reported as
  `truncated=True`, so an output of exactly 8 KB is a false positive.
- **`read_file`** streams the body and stops once `max_bytes` is exceeded.
- **`list_dir`** returns the entries sorted by name.
- **`pause` / `resume`.**
  - `pause()` and `resume()` use `docker pause` / `docker unpause`. While paused,
    every call fails at once with `SandboxError` instead of hanging on the frozen
    container.
  - Docker reports no published ports while a container is paused. So
    `connect()` on a paused sandbox returns a handle that learns its ports on
    `resume()`, and `stream_urls()` raises until then.
- **`kill()`** force-removes the container and deletes its token. It is idempotent,
  and an in-progress removal counts as success. If removal fails, the handle stays
  usable so `kill()` can be retried.
- **`stream_urls()`** returns the noVNC viewer on the loopback port:
  `http://127.0.0.1:<port>/vnc.html?view_only=1&autoconnect=1&resize=scale` and the
  same URL without `view_only=1` for interactive use.

### Cleanup

`cleanup_orphans(session_ids_alive)` removes **this worktree's** sandbox containers
whose session is not in the set, that have no session label, or whose `expires_at`
has passed. It also deletes their tokens. Containers of other worktrees are never
touched: the daemon filters by label and the provider checks the labels again. The
runtime should call it at startup and periodically. When a worktree is removed,
`scripts/superset-teardown.sh` removes all of its containers.

## Security notes (read before running sandboxes)

- **A local container is weaker isolation than a cloud VM.** The sandbox shares the
  host kernel. A kernel or Docker escape gives the attacker the host. Use the `e2b`
  provider when the content an agent handles is hostile or when isolation matters.
- **Never run sandboxes with host mounts or `--privileged`**, and never add
  capabilities beyond SETUID/SETGID. The provider never does either. Do not "fix" a
  problem by loosening the flags above.
- **Sandboxes can still reach the host through the bridge gateway.** The sandbox
  network is an ordinary bridge with the host as its gateway. What a sandbox can
  reach depends on the platform:
  - **Linux:** any host service listening on `0.0.0.0` or on the bridge interface.
    Services bound only to `127.0.0.1` are not reachable.
  - **Docker Desktop (macOS/Windows):** also host services bound **only to
    `127.0.0.1`**, through `host.docker.internal` (`192.168.65.254`). This was
    checked on 2026-11-09: from a container on `agentplatform-sandboxes`, the
    compose Postgres (`127.0.0.1:5432`) and Redis (`127.0.0.1:6379`) accepted TCP
    connections.

  Agent-controlled code inside a sandbox can therefore try to connect to the
  platform's own database, Redis, MinIO and API. **Egress mode** (`APP_SANDBOX_EGRESS=proxy`,
  off by default) closes this: see "Egress mode" below. Without it:
  - Keep strong authentication on every host service. Never rely on "it only
    listens on localhost" for anything a sandbox must not touch.
  - Do not run unrelated sensitive local services (unauthenticated databases, dev
    servers) on a machine that runs `docker_local` sandboxes with untrusted tasks.
  - Prefer `e2b` for untrusted content.
  - Remember that the sandbox also has open internet access (`network="open"`).
- **noVNC (6080) has no password.** Its only protection is the 127.0.0.1 binding.
  Never publish it on a routable interface and never proxy it without
  authentication. Share the view-only URL for watching.
- **Nothing secret goes into a sandbox.** `spec.env` is passed into the container,
  and the caller guarantees it holds no secrets. The provider adds only the
  per-container control token and the screen size.
- **Untrusted output.** Screenshots, command output, file contents and error details
  come from an environment the model controls. The host never executes anything
  from a sandbox.

## Egress mode (`APP_SANDBOX_EGRESS=proxy`)

Opt-in (T21). Start the proxy once: `docker compose --profile egress up -d egress-proxy`.

- Each sandbox stays on `agentplatform-sandboxes` (ICC off) for its loopback-published
  control and noVNC ports, and also joins its own **internal** network
  `agentplatform-egress-<random>`, shared only with the proxy (alias `egress-proxy`).
- Right after start, a short-lived helper container (the pinned proxy image, `NET_ADMIN`
  only, `--network container:<sandbox>`) deletes the sandbox's default routes and checks
  that none is left. The sandbox keeps every capability dropped, so it cannot add a route
  back. If the helper fails, the sandbox is removed (fail closed).
- `HTTP(S)_PROXY`/`http(s)_proxy` point at `http://egress-proxy:3128`; curl, pip and
  Firefox (system proxy setting) use it. `spec.env` cannot override these variables.
- The proxy (`agentplatform/egress/egress_proxy.py`, stdlib only) allows ports 80/443,
  refuses local names (`localhost`, `*.internal`, `*.local`, single-label names),
  resolves each name once, requires **every** answer to be a public address (no private,
  loopback, link-local, CGNAT, multicast or reserved address, also inside IPv6
  wrappers) and connects to the vetted address itself (no DNS rebinding).
- On kill/removal the sandbox's egress network is removed; `cleanup_orphans` also sweeps
  egress networks whose sandbox is gone. `connect()` re-attaches a recreated proxy.

Verified on Docker Desktop 28.3 (2026-11-11), from inside a real desktop: direct TCP to
`host.docker.internal:5434/6379`, `192.168.65.254:5434/6379` and `1.1.1.1:443` refused; via
the proxy `https://example.com` → 200 (also in Firefox), host Postgres/Redis → 403,
`example.com:6379` → 403; `--noproxy '*'` → no connection.

Residual: between container start and route removal (well under a second, while the
desktop boots and before any agent action) the sandbox still has its default route.

## Known issues

- **Pointer actions at the current pointer position stall for about 20 s**
  (control server, T01). `control/input.py` runs `xdotool mousemove --sync`. When
  the pointer is already at the target, no motion event arrives and xdotool waits
  for about 18–22 s before it continues. This affects `move()`, and also `click()`
  on the spot that was just clicked. Measured on 2026-11-09:
  - `move(100,100)` the first time: 0.10 s; the second time: 21.7 s.
  - `click(300,300)` the first time: 0.35 s; the second time: 18.1 s.

  The contract suite records this as a strict `xfail` for `docker_local`. The
  handle's input timeout (35 s) is longer than the server's 30 s xdotool timeout, so
  the action completes rather than erroring. Fix belongs in `sandbox-image/`.

## Open points

- **Control tokens are durable (T13).** The runtime passes a `RedisControlTokenStore`
  (`runtime/controls.py`): `{prefix}:sandbox-token:{sandbox_id}`, with a TTL of the
  sandbox's lifetime plus 15 minutes (`GuardedProvider` passes the lifetime during
  `create`). A restarted runtime can therefore `connect()` to running desktops.
  `SandboxRef` and the `agents.sandbox` column still carry no secret.
- **`spec.timeout_minutes` is not enforced** by Docker itself. It is recorded in
  `agentplatform.expires_at` and enforced by `cleanup_orphans`, which the runtime
  runs at startup and every `APP_ORPHAN_SWEEP_MIN` (default 10) minutes with the ids
  of the sessions that are still alive. It only touches containers of its own
  worktree (`APP_WORKTREE`).
- **Memory limit.** Each desktop gets `APP_DOCKER_MEM_LIMIT` (default `2g`) as its
  memory and memory+swap limit (no swap). The provider's own default stays `4g`.
- **E2B lifetime cap.** Sandbox lifetimes follow the session's `max_wall_minutes`;
  E2B requests are capped at `APP_E2B_MAX_LIFETIME_MIN` (default 60) with a
  `sandbox_lifetime_capped` warning.
