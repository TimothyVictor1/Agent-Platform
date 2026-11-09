#!/bin/bash
# Container entrypoint: prepare X authorization and defaults, then run
# supervisord in the foreground as PID 1.
set -euo pipefail

# Defaults so supervisord's %(ENV_...s interpolation never fails. An empty
# CONTROL_TOKEN puts the control server into locked mode (all requests -> 401).
export CONTROL_TOKEN="${CONTROL_TOKEN:-}"
export SCREEN_WIDTH="${SCREEN_WIDTH:-1280}"
export SCREEN_HEIGHT="${SCREEN_HEIGHT:-800}"
export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/home/agent/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/xdg-runtime}"

# The healthcheck authenticates via this root-only header file instead of
# putting the token on a command line (process args are world-readable
# through ps inside the container).
if [ -n "$CONTROL_TOKEN" ]; then
  install -m 0600 /dev/null /run/control-auth-header
  printf 'Authorization: Bearer %s\n' "$CONTROL_TOKEN" > /run/control-auth-header
fi

mkdir -p "$XDG_RUNTIME_DIR" /var/log/supervisor
chown agent:agent "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

# Per-server Xauthority cookie: only root (Xvfb) and the agent user
# (everything else) can reach the display. Not world-writable.
install -o agent -g agent -m 0600 /dev/null "$XAUTHORITY"
COOKIE="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
xauth -f "$XAUTHORITY" add ":1" . "$COOKIE" >/dev/null 2>&1
# xauth rewrites the file via a temp file + rename, which loses the
# agent ownership; restore it or every agent-user X client is rejected.
chown agent:agent "$XAUTHORITY"
chmod 600 "$XAUTHORITY"

exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
