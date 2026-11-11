# Security review (T21)

Review against SPEC §15 and AGENTS.md §5. Status: **in progress**. The egress proxy
(known issue 1) is designed and verified by hand on Docker Desktop but not implemented yet.

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

1. **High. Sandboxes reach host Postgres/Redis on Docker Desktop.** Without `APP_API_TOKEN`, a prompt-injected agent could also call the API (for example to approve its own approval request), because requests without an `Origin` header pass the origin check. Probes run on 2026-11-11 (Docker 28.3):
   - An `--internal` network blocks the host and the internet, **but published ports stop working**, so it cannot be the only network.
   - Two networks (internal plus the publishing bridge) give a default route through the bridge, so egress is open again.
   - Removing the default route with a short-lived helper (`--network container:<id> --cap-add NET_ADMIN ip route del default`) works:
     - published ports still work;
     - host.docker.internal:5434/6379, the bridge gateway and 1.1.1.1 are all blocked, including ports published on 0.0.0.0;
     - the container cannot add the route back.

   **Planned design (behind a setting that is off by default):**
   - A per-sandbox internal network that holds only the sandbox and an `egress-proxy` compose service (stdlib Python, pinned image, read-only, all capabilities dropped).
   - The proxy resolves each destination, refuses private, loopback, link-local, CGNAT 100.64/10, multicast and IPv6 equivalents (including mapped, 6to4 and NAT64), `*.internal` and `localhost`, then connects to the vetted IP (no re-resolve). Only ports 80 and 443 are allowed.
   - `HTTP(S)_PROXY` is set in the sandbox. Firefox has to be checked against `firefox-policies.json`.
   - Fail closed if the route removal fails.

   **Until then:** always set `APP_API_TOKEN`, use strong DB/Redis passwords, and prefer `e2b` for untrusted content.
2. **`NEXT_PUBLIC_API_TOKEN` ships in the browser bundle.** Anyone who can load the UI can read the token. Treat it as local-only. The real fix is server-side sessions or a BFF (follow-up).
3. **noVNC (6080) has no password.** Its only protection is the 127.0.0.1 binding and ICC being disabled. Adding a per-container VNC password needs a change to `sandbox-image/` (follow-up).
4. **The control token is visible in `docker inspect`** (`Config.Env`). Docker socket access is root-equivalent anyway. The token only guards that one container's control API. Passing it with `put_archive` before start is a follow-up.
5. **Board rate limits live in process memory.** They reset on a runtime restart, which loosens the limit only briefly; one session has exactly one owner. Accepted for v1.
6. **The E2B network allowlist is unverified** (it's unclear whether the allow rules win over the catch-all deny). Don't rely on allowlist mode until a live run confirms it. A fail-closed opt-in flag is a follow-up.
