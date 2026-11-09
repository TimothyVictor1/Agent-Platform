#!/bin/bash
# Container entrypoint: prepare the X authority cookie and the control token
# with as few root privileges as possible, then run supervisord in the
# foreground as PID 1.
#
# Designed for --cap-drop ALL --cap-add SETUID --cap-add SETGID
# --security-opt no-new-privileges: all ownership is fixed at image build
# time, root only setuid-drops to `agent` (setpriv) and never writes into
# agent-owned files itself.
set -euo pipefail

# Defaults so supervisord's %(ENV_...s) interpolation never fails.
export SCREEN_WIDTH="${SCREEN_WIDTH:-1280}"
export SCREEN_HEIGHT="${SCREEN_HEIGHT:-800}"
export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/home/agent/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/xdg-runtime}"
TOKEN_FILE="${CONTROL_TOKEN_FILE:-/run/control-token}"

# Per-server Xauthority cookie, written by the agent user itself (the empty
# file is created at build time, agent-owned mode 0600): only the agent
# user's X clients (x11vnc, the desktop, the control server) can attach.
COOKIE="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
setpriv --reuid agent --regid agent --init-groups \
    xauth -f "$XAUTHORITY" add ":1" . "$COOKIE" >/dev/null 2>&1

# Control token: relocate it out of the process environment. The control
# server reads the agent-owned 0400 file (see control/config.py); the
# healthcheck authenticates through a root-only header file instead of a
# process argument (process args are world-readable via ps). Note: the
# agent user can still read the token by design — it is a per-container
# capability, not a user secret. The file is made writable again before the
# write: it is left at 0400, so a `docker restart` would otherwise fail here.
if [ -n "${CONTROL_TOKEN:-}" ]; then
    setpriv --reuid agent --regid agent --init-groups \
        sh -c 'umask 077; chmod 0600 "$2"; printf "%s\n" "$1" > "$2"; chmod 0400 "$2"' \
        sh "$CONTROL_TOKEN" "$TOKEN_FILE"
    install -m 0600 /dev/null /run/control-auth-header
    printf 'Authorization: Bearer %s\n' "$CONTROL_TOKEN" > /run/control-auth-header
    unset CONTROL_TOKEN
fi

exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
