# CivicOS developer tasks.
.DEFAULT_GOAL := help
PYTHON ?= python
VENV   ?= .venv
BIN    := $(VENV)/bin
ifeq ($(OS),Windows_NT)
BIN := $(VENV)/Scripts
endif

.PHONY: help install dev-install run seed migrate migration test test-cov lint format typecheck check clean docker-up docker-down docker-logs

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install the package
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e .

dev-install: ## Install with dev and AI extras
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e ".[dev,anthropic,google,openai]"

run: ## Run the API with reload
	$(BIN)/civicos serve --reload

seed: ## Create the schema and a demo municipality
	$(BIN)/civicos db create-all --yes
	$(BIN)/civicos db seed

migrate: ## Apply migrations
	$(BIN)/civicos db upgrade

migration: ## Generate a migration: make migration m="add x"
	$(BIN)/python -m alembic revision --autogenerate -m "$(m)"

test: ## Run the test suite
	$(BIN)/python -m pytest -q

test-cov: ## Run tests with a coverage report
	$(BIN)/python -m pytest --cov --cov-report=term-missing --cov-report=html

lint: ## Lint
	$(BIN)/python -m ruff check src tests

format: ## Format and auto-fix
	$(BIN)/python -m ruff format src tests
	$(BIN)/python -m ruff check --fix src tests

typecheck: ## Type-check
	$(BIN)/python -m mypy src

check: lint typecheck test ## Everything CI runs

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

docker-up: ## Start the local stack
	docker compose up -d --build
	docker compose run --rm api civicos db upgrade
	docker compose run --rm api civicos db seed

docker-down: ## Stop the local stack
	docker compose down

docker-logs: ## Tail the API logs
	docker compose logs -f api
