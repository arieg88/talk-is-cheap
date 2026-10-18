.DEFAULT_GOAL := help
.PHONY: help install db db-down db-reset migrate db-check ingest lint format typecheck test test-unit check

help: ## List available commands
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## Install the package and dev tools into .venv
	uv sync

db: ## Start Postgres + pgvector in Docker and wait until it is healthy
	docker compose up -d --wait db

db-down: ## Stop the database (data is kept in a volume)
	docker compose down

db-reset: ## Stop the database and delete its data
	docker compose down -v

migrate: ## Apply all database migrations
	uv run alembic upgrade head

db-check: ## Report database connectivity, pgvector and migration status
	uv run talk-is-cheap db-check

ingest: ## Load every company in universe.yaml from SEC EDGAR, then parse filing text (needs EDGAR_IDENTITY)
	uv run talk-is-cheap ingest edgar
	uv run talk-is-cheap ingest filings

lint: ## Check lint and formatting
	uv run ruff check .
	uv run ruff format --check .

format: ## Fix lint issues and format code
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## Run strict type checking
	uv run mypy

test: ## Run all tests (integration tests skip if no database is reachable)
	uv run pytest

test-unit: ## Run tests that need no database
	uv run pytest -m "not integration"

check: lint typecheck test ## Run everything CI runs
