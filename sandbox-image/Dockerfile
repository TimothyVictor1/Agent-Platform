# syntax=docker/dockerfile:1
#
# AgentPlatform local desktop sandbox (SPEC.md §9.2).
#
#   docker build -t agentplatform/desktop:latest sandbox-image
#
# Works on linux/amd64 and linux/arm64 (BuildKit sets TARGETARCH).
#
# Supply-chain pins:
#   - ubuntu:24.04 pinned by digest (both stages)
#   - Firefox ESR pinned to a release, verified against Mozilla's
#     SHA512SUMS for that release (override with
#     --build-arg FIREFOX_VERSION=<version> to bump deliberately)
#   - control-server venv installed with pip --require-hashes
#
# Security summary (details in sandbox-image/README.md):
#   - single non-root user `agent` (uid 1000) owns the desktop and all inputs
#   - runs hardened with --cap-drop ALL --cap-add SETUID --cap-add SETGID
#     --security-opt no-new-privileges: root exists only to setuid-drop to
#     `agent`; all ownership is fixed here at build time
#   - X server blocked from TCP; Xauthority cookie restricts local clients
#   - x11vnc bound to localhost inside the container; only websockify
#     (6080) and the control server (7000) are published, on 127.0.0.1
#   - control server requires a bearer token and fails closed without one

########## stage 1: control-server venv (kept out of the desktop layer) ##########
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852 AS control-venv

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-control.txt /tmp/requirements-control.txt
COPY control /opt/control
WORKDIR /opt
RUN python3 -m venv /opt/control-venv \
    && /opt/control-venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/control-venv/bin/pip install --no-cache-dir \
        --require-hashes -r /tmp/requirements-control.txt \
    && /opt/control-venv/bin/python -c "import control.main as m; m.app" \
    && rm -f /tmp/requirements-control.txt

########## stage 2: desktop runtime ##########
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852

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
ARG FIREFOX_VERSION=153.6.0esr
RUN set -eux; \
    v="${FIREFOX_VERSION}"; \
    case "$(uname -m)" in \
      x86_64) arch=linux-x86_64 ;; \
      aarch64) arch=linux-aarch64 ;; \
      *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    base="https://ftp.mozilla.org/pub/firefox/releases/${v}"; \
    curl -fsSL --retry 4 --retry-delay 2 "${base}/SHA512SUMS" -o /tmp/SHA512SUMS; \
    curl -fsSL --retry 4 --retry-delay 2 "${base}/${arch}/en-US/firefox-${v}.tar.xz" -o /tmp/firefox.tar.xz; \
    expected="$(grep " ${arch}/en-US/firefox-${v}.tar.xz\$" /tmp/SHA512SUMS | awk '{print $1}')"; \
    test -n "$expected"; \
    echo "${expected}  /tmp/firefox.tar.xz" | sha512sum -c -; \
    mkdir -p /opt/firefox/distribution; \
    tar -xJf /tmp/firefox.tar.xz -C /opt/firefox --strip-components=1; \
    rm -f /tmp/firefox.tar.xz /tmp/SHA512SUMS; \
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

# All ownership is fixed at build time so the runtime entrypoint needs no
# chown: with --cap-drop ALL, root could not write into agent-owned files
# (no DAC_OVERRIDE) and supervisord only needs SETUID/SETGID to drop to
# `agent`. The empty token and Xauthority files are agent-owned and filled
# in by the entrypoint running as agent.
RUN userdel --remove ubuntu 2>/dev/null || true \
    && useradd --create-home --uid 1000 --user-group --shell /bin/bash agent \
    && cp -r /etc/skel/. /home/agent/ \
    && chmod +x /home/agent/Desktop/*.desktop /usr/local/bin/entrypoint.sh \
                /usr/local/bin/start-desktop.sh /usr/local/bin/start-x11vnc.sh \
                /usr/local/bin/start-novnc.sh \
    && mkdir -p /home/agent/Shared /tmp/xdg-runtime /var/log/supervisor \
    && chown -R agent:agent /home/agent \
    && chown agent:agent /tmp/xdg-runtime \
    && chmod 700 /tmp/xdg-runtime \
    && install -d -o root -g root -m 0755 /var/log/supervisor \
    && install -o agent -g agent -m 0600 /dev/null /home/agent/.Xauthority \
    && install -o agent -g agent -m 0600 /dev/null /run/control-token

EXPOSE 6080 7000

HEALTHCHECK --interval=10s --timeout=5s --start-period=90s --retries=25 \
    CMD curl -fsS -o /dev/null -H @/run/control-auth-header http://127.0.0.1:7000/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
