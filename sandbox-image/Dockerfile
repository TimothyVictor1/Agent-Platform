# syntax=docker/dockerfile:1
#
# AgentPlatform local desktop sandbox (SPEC.md §9.2).
#
#   docker build -t agentplatform/desktop:latest sandbox-image
#
# Works on linux/amd64 and linux/arm64 (BuildKit sets TARGETARCH).
# Pin Firefox/ESR for a byte-reproducible apt layer:
#   docker build --build-arg FIREFOX_VERSION=153.5.1esr ...
#
# Security summary (details in sandbox-image/README.md):
#   - single non-root user `agent` (uid 1000) owns the desktop and all inputs
#   - X server blocked from TCP; Xauthority cookie restricts local clients
#   - x11vnc bound to localhost inside the container; only websockify
#     (6080) and the control server (7000) are published, on 127.0.0.1
#   - control server requires a bearer token and fails closed without one

########## stage 1: control-server venv (kept out of the desktop layer) ##########
FROM ubuntu:24.04 AS control-venv

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-control.txt /tmp/requirements-control.txt
COPY control /opt/control
WORKDIR /opt
RUN python3 -m venv /opt/control-venv \
    && /opt/control-venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/control-venv/bin/pip install --no-cache-dir -r /tmp/requirements-control.txt \
    && /opt/control-venv/bin/python -c "import control.main as m; m.app" \
    && rm -f /tmp/requirements-control.txt

########## stage 2: desktop runtime ##########
FROM ubuntu:24.04

LABEL org.opencontainers.image.title="AgentPlatform Desktop Sandbox" \
      org.opencontainers.image.description="Ubuntu 24.04 XFCE desktop with Firefox ESR, LibreOffice and a FastAPI control server for computer-use agents" \
      org.opencontainers.image.vendor="AgentPlatform" \
      org.opencontainers.image.version="0.1.0" \
      agentplatform.image="desktop"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    SCREEN_WIDTH=1280 \
    SCREEN_HEIGHT=800 \
    DISPLAY=:1 \
    XAUTHORITY=/home/agent/.Xauthority \
    XDG_RUNTIME_DIR=/tmp/xdg-runtime \
    HOME=/home/agent

# --- desktop + tools ---------------------------------------------------------
# No power manager / screensaver / notification daemon: they would lock or
# idle out the screen an agent is looking at.
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb xauth xfce4 xfce4-terminal xfdesktop4 thunar mousepad dbus-x11 \
      x11vnc novnc websockify \
      xdotool scrot wmctrl procps \
      libreoffice-calc libreoffice-writer \
      fonts-dejavu-core fonts-liberation \
      libgtk-3-0t64 libasound2t64 libdbus-glib-1-2 libxt6t64 libgbm1 \
      libegl1 libgl1 libxcomposite1 libxdamage1 libxrandr2 libxkbcommon0 \
      libx11-xcb1 libcups2t64 \
      ca-certificates curl jq netbase xz-utils python3 python3-venv supervisor \
    && apt-get remove -y --purge xfce4-power-manager xfce4-screensaver xscreensaver \
    && apt-get -y autoremove \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# --- Firefox ESR (official Mozilla tarball; avoids the Ubuntu snap) -----------
ARG FIREFOX_VERSION=latest
RUN set -eux; \
    v="${FIREFOX_VERSION}"; \
    if [ "$v" = "latest" ]; then \
      v="$(curl -fsSL https://product-details.mozilla.org/1.0/firefox_versions.json | jq -r '.FIREFOX_ESR')"; \
    fi; \
    case "$(uname -m)" in \
      x86_64) arch=linux-x86_64 ;; \
      aarch64) arch=linux-aarch64 ;; \
      *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    url="https://ftp.mozilla.org/pub/firefox/releases/${v}/${arch}/en-US/firefox-${v}.tar.xz"; \
    curl -fsSL "$url" -o /tmp/firefox.tar.xz; \
    mkdir -p /opt/firefox/distribution; \
    tar -xJf /tmp/firefox.tar.xz -C /opt/firefox --strip-components=1; \
    rm -f /tmp/firefox.tar.xz; \
    ln -s /opt/firefox/firefox /usr/local/bin/firefox

COPY firefox-policies.json /opt/firefox/distribution/policies.json

# --- control server (venv from stage 1) ---------------------------------------
COPY --from=control-venv /opt/control-venv /opt/control-venv
COPY --from=control-venv /opt/control /opt/control

# --- supervision + desktop -----------------------------------------------------
COPY supervisord.conf /etc/supervisor/supervisord.conf
COPY desktop/ /home/agent/Desktop/
COPY scripts/start-desktop.sh scripts/start-x11vnc.sh scripts/start-novnc.sh /usr/local/bin/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

RUN useradd --create-home --uid 1000 --user-group --shell /bin/bash agent \
    && mkdir -p /home/agent/Shared \
    && chmod +x /home/agent/Desktop/*.desktop /usr/local/bin/entrypoint.sh \
                /usr/local/bin/start-desktop.sh /usr/local/bin/start-x11vnc.sh \
                /usr/local/bin/start-novnc.sh \
    && chown -R agent:agent /home/agent \
    && mkdir -p /tmp/xdg-runtime /var/log/supervisor \
    && chown agent:agent /tmp/xdg-runtime \
    && chmod 700 /tmp/xdg-runtime

EXPOSE 6080 7000

HEALTHCHECK --interval=10s --timeout=5s --start-period=90s --retries=25 \
    CMD curl -fsS -o /dev/null -H "Authorization: Bearer ${CONTROL_TOKEN}" http://127.0.0.1:7000/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
