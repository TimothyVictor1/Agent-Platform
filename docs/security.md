# Security review (T21)

Review against SPEC §15 and AGENTS.md §5. Status: done. Egress filtering for known
issue 1 is implemented and verified, but **off by default** until you turn it on.

## Fixed in T21

| # | Finding | Fix | Test |
|---|---|---|---|
| F1 | Secret hygiene only checked sandbox **env**. Commands, typed text and files a model sends were not checked (AGENTS §5: "no API keys in sandbox env, files or commands"). | `GuardedProvider` wraps every handle in `SecretGuardHandle` (`runtime/controls.py`). `run_command`, `type_text` and `write_file` that carry a configured secret raise `SandboxError` before anything is sent. The message never contains the secret. | `tests/integration/scenarios/test_security_secrets.py` |
| F2 | The secret list missed keys named by a profile's `api_key_env` that `Settings` has no field for (for example a new provider's key), and passwords written straight into `DATABASE_URL`/`REDIS_URL`. | `model_key_values(models)` plus URL passwords in `Settings.secret_values()`, merged in `build_container`. | `tests/unit/test_config.py` |
| F3 | Artifact downloads loaded the whole file (up to 50 MB) into memory (known issue 7). | `StreamingArtifactStore.open_read` (fs, S3, memory); the download route streams 64 KB chunks. | `tests/unit/artifacts/test_stores.py` |

## Checked, no change needed

- **Board text as HTML:** the frontend has no `dangerouslySetInnerHTML`, `innerHTML` or markdown renderer. React escapes all text. The noVNC iframe only accepts `http(s)` URLs.
- **CORS:** the only allowed origin is `FRONTEND_ORIGIN`, with no credentials. `OriginCheckMiddleware` rejects unsafe methods sent from any other origin, which stops cross-site form POSTs.
- **Auth:** the bearer token is compared in constant time. Only health and the API docs are exempt. The WebSocket uses single-use 60 s tickets, and the access log never contains query strings.
- **Upload limits:** the body limit applies while the body streams in (413), and the route checks the file size again. The MIME type is sniffed from the bytes.
- **Path traversal:**
  - artifact names must match `[A-Za-z0-9_][A-Za-z0-9._ -]{0,254}`;
  - sandbox paths are checked lexically under `/home/agent` and `Shared`;
  - storage keys are validated;
  - the fs store opens every path segment with `O_NOFOLLOW`;
  - downloads are always sent as an attachment, with `nosniff` and a sandbox CSP.
- **SSRF:** outbound HTTP only goes to model servers named in `models.yaml` (admin config) and to the Docker control server on 127.0.0.1. No route fetches a user-supplied URL.

## Known issues

1. **High. Sandboxes reach host Postgres/Redis on Docker Desktop.** Without
   `APP_API_TOKEN`, a prompt-injected agent could also call the API (for example to
   approve its own approval request), because requests without an `Origin` header pass
   the origin check.

   **Fix (opt-in): `APP_SANDBOX_EGRESS=proxy`** plus the compose `egress-proxy` service.
   Details in [sandboxes.md → Egress mode](sandboxes.md). In short: a per-sandbox internal
   network shared only with the proxy, the sandbox's default route removed by a NET_ADMIN
   helper (the sandbox keeps all capabilities dropped), and a proxy that resolves each
   name once, refuses any non-public address (including CGNAT and IPv6-wrapped ones) and
   connects to the vetted address. It fails closed when the proxy is missing or the route
   cannot be removed.

   Why this design: probes on Docker Desktop 28.3 showed that an `--internal` network
   alone breaks the published control/noVNC ports, `gateway_mode_ipv4=isolated` is
   allowed only on internal networks, and a second network brings the default route
   back.

   **Verified 2026-11-11** inside a real desktop:

   | Probe | Result |
   |---|---|
   | direct TCP `host.docker.internal:5434`, `:6379` | refused |
   | direct TCP `192.168.65.254:5434`, `:6379` | refused |
   | direct TCP `1.1.1.1:443` | refused |
   | `curl https://example.com` (through the proxy) | 200 |
   | Firefox headless `https://example.com` | page rendered |
   | host Postgres/Redis through the proxy | 403 |
   | `example.com:6379` | 403 (port) |
   | `curl --noproxy '*' https://example.com` | no connection |

   No containers or networks were left after the kill.

   **Residual risk:**
   - in the default `open` mode nothing changes;
   - in `proxy` mode the sandbox keeps its default route for the moment between start and
     route removal, while the desktop boots and before any agent action;
   - allowed public destinations are unrestricted, as SPEC's `network: open` intends;
   - only with `APP_EGRESS_ALLOW_EVAL_FIXTURES=1` (off by default) may the exact name
     `eval-fixtures` resolve to a private address (never loopback or link-local); every
     other private name or IP stays blocked (`tests/unit/egress`).

   Keep `APP_API_TOKEN` and strong DB/Redis passwords in either mode.
2. **`NEXT_PUBLIC_API_TOKEN` ships in the browser bundle.** Anyone who can load the UI can read the token. Treat it as local-only. The real fix is server-side sessions or a BFF (follow-up).
3. **noVNC (6080) has no password.** Its only protection is the 127.0.0.1 binding and ICC being disabled. Adding a per-container VNC password needs a change to `sandbox-image/` (follow-up).
4. **The control token is visible in `docker inspect`** (`Config.Env`). Docker socket access is root-equivalent anyway. The token only guards that one container's control API. Passing it with `put_archive` before start is a follow-up.
5. **Board rate limits live in process memory.** They reset on a runtime restart, which loosens the limit only briefly; one session has exactly one owner. Accepted for v1.
6. **The E2B network allowlist is unverified** (it's unclear whether the allow rules win over the catch-all deny). Don't rely on allowlist mode until a live run confirms it. A fail-closed opt-in flag is a follow-up.
