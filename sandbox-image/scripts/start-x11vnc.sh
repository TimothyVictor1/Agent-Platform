#!/bin/bash
# VNC server for the Xvfb display. -localhost keeps the VNC protocol port
# unreachable from other containers on the sandbox network; noVNC users connect
# through websockify on 6080 instead.
set -euo pipefail
export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/home/agent/.Xauthority}"

until xset -q >/dev/null 2>&1; do sleep 0.3; done
exec x11vnc -display :1 -rfbport 5900 -localhost -forever -shared \
    -noxdamage -nopw -norc -xkb -quiet
