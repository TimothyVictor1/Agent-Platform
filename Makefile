# Developer entry points (SPEC §5.3). Run from the repo root.
# Per-worktree settings (ports, DB name) come from .env, written by scripts/superset-setup.sh.

SHELL := /bin/bash
# Read .env only for the few values make itself needs. Do not blanket-export it:
# the backend and frontend load .env themselves (with proper dotenv parsing).
-include .env
APP_DB_NAME := $(strip $(or $(APP_DB_NAME),agentplatform))
PORT := $(strip $(or $(PORT),3000))
PNPM ?= $(shell command -v pnpm >/dev/null 2>&1 && echo pnpm || echo "corepack pnpm")
BACKEND := cd backend &&
FRONTEND := cd frontend &&
COMPOSE := docker compose

.PHONY: help dev api runtime web test test-backend test-frontend test-evals lint lint-backend \
	lint-frontend lint-sandbox typecheck format contracts contracts-check sandbox-image \
	db-up db-down db-migrate debug-bundle install

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-16s %s\n", $$1, $$2}'

install: ## Install backend (uv) and frontend (pnpm) dependencies
	$(BACKEND) uv sync
	$(FRONTEND) $(PNPM) install

dev: db-up db-migrate ## Run API + runtime + web together (ports from .env); Ctrl-C stops all
	@trap 'kill 0' INT TERM; \
	($(MAKE) --no-print-directory api 2>&1 | awk '{ print "[api]     " $$0; fflush() }') & \
	($(MAKE) --no-print-directory runtime 2>&1 | awk '{ print "[runtime] " $$0; fflush() }') & \
	($(MAKE) --no-print-directory web 2>&1 | awk '{ print "[web]     " $$0; fflush() }') & \
	wait

api: ## Run the API service
	$(BACKEND) uv run agentplatform-api

runtime: ## Run the runtime service
	$(BACKEND) uv run agentplatform-runtime

web: ## Run the Next.js dev server on $$PORT
	$(FRONTEND) $(PNPM) dev --port $(PORT)

test: test-backend test-frontend test-evals ## Run all tests (no network, no Docker)

test-backend:
	$(BACKEND) uv run pytest

test-frontend:
	$(FRONTEND) $(PNPM) test

test-evals:
	uv run --project backend python -m pytest evals/tests

lint: lint-backend lint-frontend ## Lint + format check + typecheck everything

lint-backend:
	$(BACKEND) uv run ruff check .
	$(BACKEND) uv run ruff format --check .
	$(BACKEND) uv run mypy src tests

lint-frontend:
	$(FRONTEND) $(PNPM) lint
	$(FRONTEND) $(PNPM) typecheck

lint-sandbox: ## Ruff + ruff format + mypy --strict for the sandbox control server
	$(BACKEND) uv run ruff check ../sandbox-image/control
	$(BACKEND) uv run ruff format --check ../sandbox-image/control
	$(BACKEND) uv run mypy --strict ../sandbox-image/control

typecheck: ## Type-check backend (mypy --strict) and frontend (tsc)
	$(BACKEND) uv run mypy src tests
	$(FRONTEND) $(PNPM) typecheck

format: ## Auto-format backend code
	$(BACKEND) uv run ruff check --fix .
	$(BACKEND) uv run ruff format .

contracts: ## Regenerate JSON Schema + TypeScript contracts from Pydantic
	$(BACKEND) uv run python ../scripts/gen_contracts.py
	$(FRONTEND) node scripts/gen-contracts.mjs

contracts-check: ## Fail if generated contracts are stale
	@tmp=$$(mktemp -d); \
	(cd backend && uv run python ../scripts/gen_contracts.py --out $$tmp/schema.json >/dev/null) && \
	(cd frontend && node scripts/gen-contracts.mjs --schema $$tmp/schema.json --out $$tmp/generated.ts >/dev/null) && \
	diff -q $$tmp/schema.json frontend/src/contracts/schema.json >/dev/null && \
	diff -q $$tmp/generated.ts frontend/src/contracts/generated.ts >/dev/null; \
	status=$$?; rm -rf $$tmp; \
	if [ $$status -ne 0 ]; then echo "contracts are stale: run 'make contracts' and commit"; exit 1; fi; \
	echo "contracts are up to date"

sandbox-image: ## Build the local desktop sandbox image
	docker build -t agentplatform/desktop:latest sandbox-image

db-up: ## Start Postgres + Redis and create this worktree's database
	$(COMPOSE) up -d --wait postgres redis
	@$(COMPOSE) exec -T postgres psql -U agentplatform -d agentplatform -tAc \
		"SELECT 1 FROM pg_database WHERE datname = '$(APP_DB_NAME)'" | grep -q 1 \
		|| $(COMPOSE) exec -T postgres createdb -U agentplatform "$(APP_DB_NAME)"
	@echo "database $(APP_DB_NAME) ready"

db-down: ## Stop Postgres + Redis (data volumes are kept)
	$(COMPOSE) down

db-migrate: ## Apply Alembic migrations to this worktree's database
	$(BACKEND) uv run alembic upgrade head

debug-bundle: ## Export a session's events/board/results/frames (stub; SPEC §19)
	@echo "debug-bundle is not implemented yet (SESSION=$(SESSION))"; exit 1
