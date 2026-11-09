#!/bin/bash
# noVNC websocket bridge: serves the noVNC web app and proxies VNC to x11vnc.
set -euo pipefail
exec /usr/bin/websockify --web /usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900
