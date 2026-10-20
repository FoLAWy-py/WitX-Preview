.PHONY: setup check lint format typecheck test test-python test-web contracts build \
        db-migrate dev-api dev-worker dev-web dev-gateway

setup:            ## install all dependencies
	uv sync --locked
	pnpm install --frozen-lockfile

check: lint typecheck test build   ## everything CI runs

lint:
	uv run --locked ruff check services packages scripts tests
	uv run --locked ruff format --check services packages scripts tests
	uv run --locked lint-imports
	pnpm lint:web
	pnpm format:check:web

format:
	uv run --locked ruff format services packages scripts tests
	uv run --locked ruff check --fix services packages scripts tests
	pnpm format:web

typecheck:
	uv run --locked mypy
	pnpm typecheck:web

test: test-python test-web

test-python:      ## needs PostgreSQL: see tests/conftest.py
	uv run --locked pytest
	uv run --locked python scripts/export_contracts.py --check

test-web:
	uv run --locked python scripts/third_party_notices.py --check
	pnpm test:web
	pnpm test:browser:web

contracts:        ## regenerate apps/web/src/api/types.gen.ts
	uv run --locked python scripts/export_contracts.py

build:
	pnpm build:web
	uv build --all-packages

db-migrate:
	uv run --locked witx migrate

dev-api:
	uv run --locked witx api

dev-worker:
	uv run --locked witx worker

dev-web:
	pnpm dev:web

dev-gateway:      ## GATEWAY_CONFIG=path/to/gateway.toml
	uv run --locked witx-gateway --config $(GATEWAY_CONFIG)
