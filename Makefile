.DEFAULT_GOAL := help
.PHONY: help install db db-down db-reset migrate db-check ingest index extract eval-retrieval eval-guidance lint format typecheck test test-unit check

help: ## List available commands
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-15s %s\n", $$1, $$2}'

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

ingest: ## Load every company from SEC EDGAR, parse filing text, index it, and extract guidance if an LLM key is set (needs EDGAR_IDENTITY)
	uv run talk-is-cheap ingest edgar
	uv run talk-is-cheap ingest filings
	uv run talk-is-cheap index
	@if uv run python -c "import sys; from talk_is_cheap.config import get_settings; sys.exit(0 if get_settings().llm_api_key else 1)"; then \
		uv run talk-is-cheap extract guidance; \
	else \
		echo "no LLM_API_KEY or ANTHROPIC_API_KEY set; skipping guidance extraction"; \
	fi

index: ## Chunk and embed new or changed filing text and news for search
	uv run talk-is-cheap index

extract: ## Extract guidance from new or changed outlook sections and refresh verdicts (needs an LLM key unless cached)
	uv run talk-is-cheap extract guidance

eval-retrieval: ## Recall and MRR of each search mode on evals/retrieval/questions.jsonl
	uv run talk-is-cheap eval retrieval --details

eval-guidance: ## Precision, recall, figures and periods of guidance extraction (LLM_MODEL) on evals/guidance/labels.jsonl
	uv run talk-is-cheap eval guidance --details

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
