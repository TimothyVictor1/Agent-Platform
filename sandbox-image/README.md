# Desktop sandbox image (`agentplatform/desktop`)

The local desktop sandbox for AgentPlatform (SPEC.md §9.2, task T01). Ubuntu
24.04 with an XFCE4 desktop, Firefox ESR, LibreOffice (Calc + Writer),
mousepad and a small FastAPI **control server** (port 7000) that the
`docker_local` provider (task T07) drives over HTTP.

## Build

```sh
docker build -t agentplatform/desktop:latest sandbox-image
# pinned/reproducible Firefox layer:
docker build -t agentplatform/desktop:latest \
  --build-arg FIREFOX_VERSION=153.5.1esr sandbox-image
```

Multi-arch: the same Dockerfile builds on `linux/amd64` and `linux/arm64`
(Firefox ESR tarballs are fetched per architecture; everything else comes
from Ubuntu archives for both).

## Run

```sh
TOKEN=$(openssl rand -hex 32)
docker run -d --name ap-desktop \
  -e CONTROL_TOKEN="$TOKEN" \
  -p 127.0.0.1::7000 -p 127.0.0.1::6080 \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory 4g --cpus 2 \
  agentplatform/desktop:latest
docker port ap-desktop          # find the random host ports
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:<7000-port>/health
```

`CONTROL_TOKEN` must be set at run time: the provider generates a random
token per container (`secrets.token_urlsafe`). **With no token the control
server fails closed** — every request, including `/health`, returns 401.
The token is a per-container runtime capability, not a user secret; it must
never contain or grant access to API keys (nothing secret is ever sent into
a sandbox).

### Ports

| Port (container) | Purpose                                             |
| ---------------- | --------------------------------------------------- |
| 7000             | control server (HTTP, bearer token)                 |
| 6080             | noVNC (browser streaming, served by websockify)     |

Publish **only on 127.0.0.1** with random host ports (as above; the
provider does this automatically).

### noVNC URLs

Given the published 6080 port:

- view-only: `/vnc.html?view_only=1&autoconnect=1&resize=scale`
- interactive: `/vnc.html?autoconnect=1&resize=scale`

### Control server endpoints

`Authorization: Bearer $CONTROL_TOKEN` on every call.

| Method | Path              | Body / params                | Result                          |
| ------ | ----------------- | ---------------------------- | ------------------------------- |
| GET    | `/health`         | –                            | `{"status":"ok"}`               |
| GET    | `/screen`         | –                            | `{"width":1280,"height":800}`   |
| GET    | `/screenshot`     | –                            | PNG bytes                       |
| POST   | `/click`          | `x,y,button,count`           | ok                              |
| POST   | `/move`           | `x,y`                        | ok                              |
| POST   | `/drag`           | `x1,y1,x2,y2`                | ok (interpolated LMB drag)      |
| POST   | `/scroll`         | `x,y,direction,amount`       | ok                              |
| POST   | `/type`           | `text,interval_ms`           | ok (≤50 000 chars, total ≤5 min)|
| POST   | `/key`            | `combo`                      | ok (`ctrl+l`, `Ctrl + Shift + T`)|
| POST   | `/command`        | `cmd,timeout_s`              | `{stdout,stderr,exit_code,timed_out}` |
| POST   | `/files/write`    | multipart `path` + `file`    | `{path,bytes}` (≤50 MB)         |
| GET    | `/files/read`     | `?path=/abs/path`            | raw bytes (≤50 MB)              |
| GET    | `/files/list`     | `?path=/abs/dir`             | `{path,entries:[...]}`          |

File paths must be absolute **and stay under `/home/agent`** (the agent's
home): traversal such as `/home/agent/../..` is rejected and symlinked paths
are resolved before use. The root can be moved with `SANDBOX_FILE_ROOT` if a
provider needs a different layout. `/command` still grants full agent-level
shell access by design — the container is the security boundary, not the file
API.

`/command` runs through `bash -lc` **as the `agent` user** with cwd
`/home/agent`; stdout/stderr are truncated to 8 KB each and the whole
process group is killed on timeout (exit code 124, `timed_out: true`).

## Inside the container

- supervisord manages: Xvfb (:1, `$SCREEN_WIDTH`x`$SCREEN_HEIGHT`x24),
  the XFCE4 session, x11vnc (localhost:5900), websockify/noVNC (6080) and
  the control server (7000).
- Desktop launchers: Browser, Terminal, Files, LibreOffice Calc, LibreOffice
  Writer (in `/home/agent/Desktop`).
- Firefox ESR policies (`/opt/firefox/distribution/policies.json`): no
  telemetry/studies/Pocket/updates/crash reporter, no default-browser
  check, homepage locked to `about:blank`, no session-restore nag. No
  screensaver, power management, screen locking or update notifiers are
  installed, so the screen never blanks while an agent works.
- `SCREEN_WIDTH`/`SCREEN_HEIGHT` (default 1280x800) resize Xvfb **and** the
  control server's coordinate validation — pass both together.

## Tests

```sh
APP_DOCKER=1 python3 -m pytest sandbox-image/tests -q
# optionally force a build first / point at another tag:
APP_DOCKER=1 APP_TEST_IMAGE=agentplatform/desktop:latest APP_TEST_BUILD=1
```

The suite builds the image if missing, starts one container, and checks:
health, auth (401 without/with a wrong token), screenshot PNG is 1280x800,
typing into mousepad then reading the file back through `/command`,
keyboard-combo editing, file write/read/list roundtrip, 404/422/413 file
validation, command truncation and timeout kill, scroll behaviour, and that
both noVNC entry URLs are served.

## Security notes (read before exposing anything)

- **Isolation is weaker than a cloud VM.** This is a Linux container on the
  host kernel. Treat it as a convenience sandbox, not a strong security
  boundary; use the `e2b` provider when isolation matters.
- **Never mount host volumes** into a sandbox container
  (`-v /host/...:/...` defeats the isolation entirely).
- **Never run privileged** (`--privileged`), never add capabilities
  (`--cap-add`). The provider runs `--cap-drop ALL --security-opt
  no-new-privileges` with memory/CPU caps; preserve that.
- The control server is the only API into the container and requires the
  bearer token on every request (constant-time compare). Do not publish
  port 7000 or 6080 on anything but 127.0.0.1; the VNC port (5900) is bound
  to localhost inside the container and is unreachable from the host or the
  sandbox network. Inside the container x11vnc listens on `127.0.0.1:5900`
  only; websockify (6080) and the control server (7000) must bind
  `0.0.0.0` for Docker port publishing to work, which is why publishing
  them on 127.0.0.1 host-side is mandatory.
- Token hygiene inside the container: the healthcheck authenticates through
  a root-only header file (`/run/control-auth-header`), not a command line
  (process args are world-readable via `ps`), and `/command` strips
  `CONTROL_TOKEN` from the environment it hands to model-supplied
  commands. The token is still a per-container capability the `agent` user
  could locate (e.g. `/proc` of the control server) — by design, it guards
  the container's own control API and must never be a user secret.
- No secrets are baked into the image: no token, key or credential in any
  layer (the only build args are public), the Xauthority cookie is
  generated at container start, and nothing secret is ever passed into a
  sandbox (AGENTS.md §5).
- No secrets ever belong inside the sandbox: no API keys in env, files or
  commands (AGENTS.md §5).
- Untrusted content is expected inside the desktop (web pages, files,
  model-written text). Nothing from inside the container is executed on the
  host — the host never runs commands produced by models; only the sandbox
  runs them.
- X access is restricted by a per-container Xauthority cookie; Xvfb does
  not listen on TCP (`-nolisten tcp`).
