#!/bin/bash
# Wait for Xvfb, disable screensaver/power management, then run the XFCE4
# session. Runs as the agent user under supervisord.
set -euo pipefail
export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/home/agent/.Xauthority}"

until xset -q >/dev/null 2>&1; do sleep 0.3; done
xset -dpms || true
xset s off || true
xset s noblank || true

# dbus-launch creates the session bus XFCE needs and dies with the session,
# so supervisord restarts the whole desktop coherently.
exec dbus-launch --exit-with-session startxfce4
