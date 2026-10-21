#!/usr/bin/env bash
# Runs when Superset creates a new worktree for a task.
# 1) copies .env from the main worktree, 2) gives this worktree its own ports and DB,
# 3) installs dependencies. Safe to run more than once.
set -euo pipefail

# Superset provides SUPERSET_ROOT_PATH (main repo) and SUPERSET_WORKSPACE_NAME; fall back to git if absent.
MAIN_WORKTREE="${SUPERSET_ROOT_PATH:-$(git worktree list --porcelain | awk '/^worktree /{print $2; exit}')}"
HERE="$(pwd)"
NAME="$(printf '%s' "${SUPERSET_WORKSPACE_NAME:-$(basename "$HERE")}" | tr -c 'a-zA-Z0-9_' '_')"

# 1) .env from the main checkout (never committed)
if [ ! -f .env ] && [ -f "$MAIN_WORKTREE/.env" ]; then
  cp "$MAIN_WORKTREE/.env" .env
fi
[ -f .env ] || cp .env.example .env

# 2) unique ports + database per worktree so several agents can run dev servers at once
if ! grep -q '^# superset-worktree' .env; then
  OFFSET=$(( $(printf '%s' "$NAME" | cksum | cut -d' ' -f1) % 50 ))
  API_PORT=$((8100 + OFFSET))
  WEB_PORT=$((3100 + OFFSET))
  {
    echo ""
    echo "# superset-worktree $NAME"
    echo "APP_API_PORT=$API_PORT"
    echo "PORT=$WEB_PORT"
    echo "FRONTEND_ORIGIN=http://localhost:$WEB_PORT"
    echo "NEXT_PUBLIC_API_URL=http://localhost:$API_PORT"
    echo "NEXT_PUBLIC_WS_URL=ws://localhost:$API_PORT"
    echo "APP_DB_NAME=agentplatform_$NAME"
    echo "APP_WORKTREE=$NAME"
  } >> .env
fi

# 3) dependencies (skip quietly if the folders do not exist yet, e.g. before T00 is merged)
if [ -f backend/pyproject.toml ]; then (cd backend && uv sync); fi
if [ -f frontend/package.json ]; then (cd frontend && pnpm install --frozen-lockfile || pnpm install); fi

echo "Superset setup done for $NAME"
