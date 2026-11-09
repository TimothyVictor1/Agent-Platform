# `e2b` sandbox provider (cloud)

Code: `backend/src/agentplatform/sandboxes/e2b_provider.py` · Spec: SPEC §9.1, §9.3 · Task: T08

The `e2b` provider runs each agent's desktop in an [E2B](https://e2b.dev) cloud sandbox
(a Firecracker micro-VM running the `desktop` template: Xvfb + XFCE + xdotool + noVNC). It
implements the same `SandboxProvider` / `SandboxHandle` protocol as `docker_local`. It is
built on the **E2B Desktop Python SDK** (`e2b-desktop`, sync API), with each blocking call
run in `asyncio.to_thread`.

## Setup

1. Create an E2B account and an API key.
2. Put the key in the backend `.env` as `E2B_API_KEY=...`. That is the only place it
   lives. Never commit it.
3. Choose `e2b` as the sandbox provider for the session.

`E2BProvider.health()` reports `ok=false` with `E2B_API_KEY is not set` when the key is
missing. Otherwise it makes one cheap `Sandbox.list(limit=1)` call.

## Free tier and limits

What the free (Hobby) tier gives you:

| Limit | Value |
| --- | --- |
| Usage credit | one-time only. When it runs out, sandboxes cannot be created until you add billing. Check the current amount and the concurrency limit on E2B's pricing page. |
| Maximum sandbox lifetime | **1 hour** on Hobby, 24 hours on Pro (stated by SDK `e2b-desktop` 2.7.1, `Sandbox.create(timeout=...)`) |
| Billing | per second while a sandbox is running, so kill sandboxes you don't need. |

How the provider deals with those limits:

- **Timeout passed through.** `SandboxSpec.timeout_minutes` is passed to E2B as the
  sandbox timeout in seconds. On Hobby, a value above 60 is rejected by E2B, and `create`
  raises `SandboxError`. The provider does not shorten the value on its own.
- **Expiry tracking.** The handle stores `expires_at`, taken from the requested timeout on
  `create` and from E2B's `end_at` on `connect`/`resume`. `time_left()` returns the
  remaining time. The first sandbox call with **5 minutes or less** left logs one
  `sandbox_expiring` warning (`sandbox_id`, `seconds_left`). The scheduler/supervisor can
  use `time_left()` to wrap up or checkpoint.
- **After expiry.** When E2B ends the sandbox, every call raises `SandboxExpiredError`.

## Method mapping

| Handle method | SDK call |
| --- | --- |
| `screenshot()` | `screenshot()` (scrot PNG) |
| `click(x, y, button, count)` | `move_mouse(x, y)`, then `left_click`/`right_click`/`middle_click`, or `double_click` for left x2. Other counts use `xdotool click --repeat N <btn>`. |
| `move`, `drag` | `move_mouse`, `drag((x1, y1), (x2, y2))` |
| `scroll` | up/down: `move_mouse` + `scroll(direction, amount)`. left/right: `xdotool click --repeat N 6/7` (the SDK has no horizontal scroll). |
| `type_text(text, interval_ms)` | `write(text, delay_in_ms=interval_ms)` |
| `key("ctrl+shift+t")` | `press(["ctrl", "shift", "t"])` (the SDK maps names to X keysyms) |
| `run_command(cmd, timeout_s)` | `commands.run("timeout -k 5 <t> bash -lc '<cmd>'", timeout=t+15)` |
| `write_file`, `read_file`, `list_dir` | `files.write`, `files.get_info` + `files.read(format="bytes")`, `files.get_info` + `files.list(depth=1)` |
| `stream_urls()` | `stream.start(require_auth=True)`, `stream.get_url(view_only=…, auth_key=…)` |
| `pause()` / `resume()` | `pause()` / `Sandbox.connect(id)` (resumes a paused sandbox) |
| `kill()` | `kill()` (idempotent: a sandbox that is already gone counts as killed) |

Details:

- **Clicks.** The SDK's click helpers skip the pointer move when `x` or `y` is `0`, so the
  handle always moves first and then clicks where the pointer is.
- **Concurrency.** Input actions on one handle are serialised by a lock, so a drag or
  move+click never interleaves with another action. Screenshots, commands and file calls
  run concurrently: a live-tile screenshot does not wait for a long command.
- **`run_command` results.**
  - A non-zero exit is returned in `exit_code`, never raised.
  - Exit code `124` means `timed_out=true`; it is also returned if the SDK's own deadline
    fires first.
  - stdout and stderr are each cut to 8 KB (`truncated=true`).
  - Commands run as the template's default user `user`.
- **Keys.** Each part of a combo must match `[a-z0-9_]+`. Anything else raises
  `SandboxError` before reaching the SDK, which formats key names into a shell command
  without quoting them. Names xdotool doesn't know fail inside the sandbox and also raise
  `SandboxError`.
- **Pause.** Pause keeps memory and filesystem (`mode="full"`). While paused, every
  action and read raises `SandboxError`. `stream_urls()` still returns the cached URLs,
  and they work again after `resume()`.

## Reconnecting after a runtime restart

`E2BProvider.connect(id)` reattaches with `Sandbox.connect`. It also resumes the sandbox
if it was paused. It then reads `end_at` and the screen size from the sandbox.

The desktop SDK does not rebuild its stream object on `connect`, and the old stream
password is not stored anywhere. So the first `stream_urls()` after a reconnect restarts
x11vnc and noVNC inside the sandbox with a **new** random password. URLs handed out before
the restart stop working; clients always fetch fresh URLs from the API.

## Error mapping

| SDK exception | Platform error |
| --- | --- |
| `SandboxNotRunningException`, `SandboxNotFoundException` / `NotFoundException` | `SandboxExpiredError` |
| `FileNotFoundException` | `SandboxError` ("no such file or directory") |
| `AuthenticationException` | `SandboxError` ("e2b rejected the API key") |
| `RateLimitException`, `TimeoutException`, `SandboxUnreachableException`, `NotEnoughSpaceException`, `InvalidArgumentException`, `CommandExitException`, other `SandboxException` | `SandboxError` with a short, fixed message |
| anything else | `SandboxError` naming only the exception type |

Messages never include SDK text, URLs or the API key. The original exception is chained
(`__cause__`) for debugging, and only its type is logged.

## Security notes

- **API key.**
  - The key is read only from `E2B_API_KEY` in the backend environment and passed per
    call as `api_key=`. It is never put into the sandbox env, files or commands.
  - `create` refuses a `SandboxSpec.env` in which any value contains the key.
  - The key is never logged or put into exception messages.
- **Isolation.** Each sandbox is its own micro-VM. That is stronger isolation than a
  `docker_local` container, so the template's default user having root inside the VM does
  not put the host at risk. Still, treat everything inside the sandbox (screen contents,
  command output, files) as untrusted data.
- **Stream access.**
  - Streams always require a password (`require_auth=True`). The password is random and
    made for this sandbox alone, and it is embedded in both URLs. It is a per-sandbox
    access token, not a platform secret, but anyone holding the interactive URL can
    control the desktop.
  - Only hand the interactive URL to the session owner during takeover.
  - Don't log either URL.
- **Network.** `SandboxSpec.network="allowlist"` becomes E2B network rules
  `allow_out=<allowlist>` plus `deny_out=["0.0.0.0/0"]`. Entries may be IPs, CIDR blocks
  or hostnames. **Not yet verified:** that the allow rules win over the catch-all deny.
  The SDK docs don't say so. Confirm it with a live run before relying on allowlist mode
  with this provider.
- **Late cleanup on timeout.** If `create` gives up after its timeout (60 s by default)
  while the SDK call is still running in its worker thread, the provider kills the
  sandbox as soon as that call returns. Without this, an orphaned VM would keep running
  and billing.

## Tests

- **Unit tests:** `backend/tests/unit/sandboxes/test_e2b_provider.py`. The SDK is
  replaced with an in-memory fake, so the tests make no network calls. They cover:
  - method mapping and key combos
  - bounds checks
  - `run_command` exit codes, timeout and truncation
  - files
  - error mapping and secret hygiene
  - pause/resume/kill
  - reconnect and stream restart
  - expiry warning
  - late cleanup after a create timeout
  - health
- **Live contract suite:** the shared suite
  `backend/tests/integration/test_sandbox_contract.py` runs the `e2b` parameter only when
  `APP_LIVE=1` and `E2B_API_KEY` are set. It builds the provider with
  `make_e2b_contract_provider()`. A live run uses real E2B credit.
