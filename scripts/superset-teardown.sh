#!/usr/bin/env bash
# Runs when Superset removes a worktree. Stops this worktree's sandbox containers.
set -euo pipefail
NAME="$(printf '%s' "${SUPERSET_WORKSPACE_NAME:-$(basename "$(pwd)")}" | tr -c 'a-zA-Z0-9_' '_')"
if command -v docker >/dev/null 2>&1; then
  docker ps -aq --filter "label=agentplatform.worktree=$NAME" | xargs -r docker rm -f >/dev/null || true
fi
echo "Superset teardown done for $NAME"
