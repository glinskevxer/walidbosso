# Everything a developer runs, in one place, so CI and a laptop agree.

PYTHON ?= python3
VENV   ?= .venv
BIN    := $(VENV)/bin
PKG    := src/binyard
DB     ?= binyard.sqlite3

.DEFAULT_GOAL := help

.PHONY: help
help: ## list the targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

.PHONY: install
install: $(BIN)/python ## install the package and the dev tools
	$(BIN)/pip install -e ".[api,dev]"

.PHONY: test
test: ## run the suite
	$(BIN)/pytest

.PHONY: cover
cover: ## run the suite with coverage
	$(BIN)/pytest --cov=binyard --cov-report=term-missing --cov-report=xml

.PHONY: fast
fast: ## run everything except the slow properties
	$(BIN)/pytest -m "not slow"

.PHONY: lint
lint: ## ruff, in check mode
	$(BIN)/ruff check $(PKG) tests scripts
	$(BIN)/ruff format --check $(PKG) tests scripts

.PHONY: format
format: ## ruff, fixing what it can
	$(BIN)/ruff check --fix $(PKG) tests scripts
	$(BIN)/ruff format $(PKG) tests scripts

.PHONY: types
types: ## mypy over the package
	$(BIN)/mypy $(PKG)

.PHONY: release-check
release-check: ## refuse a tree that is not releasable
	$(BIN)/python scripts/check_release.py

.PHONY: check
check: lint types test release-check ## everything CI runs

.PHONY: migrate
migrate: ## bring $(DB) up to the shipped schema
	$(BIN)/binyard --database sqlite:///$(DB) migrate

.PHONY: seed
seed: ## build a demo yard with a season of loads in it
	$(BIN)/python scripts/seed_demo.py --database $(DB) --force

.PHONY: serve
serve: ## run the api on 8000 with reload
	$(BIN)/uvicorn --factory binyard.api:create_app --reload --port 8000

.PHONY: clean
clean: ## remove build and cache droppings
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov coverage.xml .coverage
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

.PHONY: distclean
distclean: clean ## also remove the venv and the demo database
	rm -rf $(VENV) $(DB)
