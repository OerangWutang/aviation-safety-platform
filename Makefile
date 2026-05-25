# Atlas Backend — development task runner
# Usage: make <target>
# All targets assume you have run: pip install -e '.[dev]'

.PHONY: help install lock dev up down migrate bootstrap docker-build docker-run-prod load-test \
        test test-unit test-integration lint format typecheck \
        check ci-local ci release-check clean free-build free-preflight free-disk free-up free-down \
        free-backup free-backup-check free-bootstrap release-clean

PYTHON   := python
PYTEST   := PYTHONPATH=src:. pytest
RUFF     := ruff
MYPY     := PYTHONPATH=src mypy

help:           ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ── Installation ─────────────────────────────────────────────────────────────

install:        ## Install from lockfile (run 'make lock' first if missing)
	@if [ ! -f requirements-dev.txt ]; then \
	  echo "ERROR: requirements-dev.txt not found. Run 'make lock' first."; \
	  exit 1; \
	fi
	pip install -r requirements-dev.txt
	pip install -e . --no-deps

lock:           ## Pin dependencies via pip-tools (generates requirements*.txt)
	@command -v pip-compile >/dev/null 2>&1 || pip install pip-tools
	pip-compile --no-emit-index-url --no-emit-trusted-host --strip-extras -o requirements.txt requirements.in
	pip-compile --no-emit-index-url --no-emit-trusted-host --strip-extras -o requirements-dev.txt requirements-dev.in
	@echo "Lockfiles updated — commit requirements.txt and requirements-dev.txt"

# ── Docker / database ────────────────────────────────────────────────────────

up:             ## Start the postgres (and optional redis) containers
	docker compose up -d db
	@echo "Tip: run 'docker compose --profile full up -d' to also start redis and the API container"

down:           ## Stop all containers
	docker compose down

migrate:        ## Apply all pending Alembic migrations
	alembic upgrade head

migrate-check:  ## Destructively verify migrations from base; requires explicit opt-in
	@test "$$ATLAS_ALLOW_DB_RESET" = "1" || \
	  (echo "Refusing to downgrade the configured DB. Re-run with ATLAS_ALLOW_DB_RESET=1 against a disposable database."; exit 1)
	@echo "Checking migrations from scratch inside the API container against the configured disposable DB..."
	docker compose --profile full run --rm api \
	  sh -c "alembic downgrade base && alembic upgrade head"

bootstrap:      ## Seed the CuratorOverride source and create a dev API key
	atlas bootstrap --role admin

docker-build:   ## Verify the production container image builds
	docker build -t atlas-backend:latest .

docker-run-prod: ## Run the production ASGI wrapper locally after building
	docker run --rm -p 8000:8000 --env-file .env atlas-backend:latest

free-build:     ## Build image used by deploy/free self-hosted stack
	docker build -t atlas-backend:local .

free-preflight: ## Validate deploy/free/.env refuses unsafe production defaults
	cd deploy/free && ./check-env.sh .env

free-disk:      ## Check local disk pressure before/after free deployment
	cd deploy/free && ./check-disk.sh .

free-up:        ## Start the free/self-hosted stack from deploy/free/.env
	cd deploy/free && ./check-env.sh .env && docker compose --env-file .env up -d

free-down:      ## Stop the free/self-hosted stack
	cd deploy/free && docker compose --env-file .env down

free-backup:    ## Write a compressed Postgres backup under deploy/free/backups
	cd deploy/free && ./backup-postgres.sh

free-backup-check: ## Fail if the newest free-deploy backup is too old or missing
	cd deploy/free && ./check-latest-backup.sh

free-bootstrap: ## Create the first admin API key in the free/self-hosted stack
	cd deploy/free && docker compose --env-file .env run --rm api atlas bootstrap --role admin

load-test:      ## Run the k6 operational load test; requires k6 and env vars
	k6 run ops/load/atlas_k6_load_test.js

# ── Testing ──────────────────────────────────────────────────────────────────

test:           ## Run default tests; integration and release tests are skipped unless enabled
	$(PYTEST) --no-cov -m "not integration and not release"

test-unit:      ## Run only unit/smoke tests (no DB required)
	$(PYTEST) --no-cov -m "not integration and not release"

test-integration: ## Run DB-backed integration tests (requires make up && make migrate)
	$(PYTEST) -m integration --run-integration -v

test-cov:       ## Run tests with coverage report
	$(PYTEST) --cov=src/atlas --cov-report=term-missing --cov-report=html

# ── Static analysis ──────────────────────────────────────────────────────────

lint:           ## Check style with ruff
	$(RUFF) check .

format:         ## Auto-fix ruff style issues
	$(RUFF) check --fix .
	$(RUFF) format .

format-check:   ## Check formatting without modifying files
	$(RUFF) format --check .

typecheck:      ## Run mypy strict type checking
	$(MYPY) src

# ── Combined checks ──────────────────────────────────────────────────────────

check:          ## Run lint + format-check + typecheck + unit tests (fast, no DB)
	$(PYTHON) -m compileall -q src tests alembic
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) src
	$(PYTEST) --no-cov -m "not integration and not release"

ci-local:       ## Mirror the fast CI path locally (no Docker/PostGIS)
	$(PYTHON) -m compileall -q src tests alembic
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) src
	$(PYTEST) --no-cov -m "not integration and not release"

ci:             ## Full CI suite including integration tests (requires DB)
	$(PYTHON) -m compileall -q src tests alembic
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) src
	$(PYTEST) --no-cov --run-integration -m "not release"

# ── Cleanup ──────────────────────────────────────────────────────────────────

release-check:  ## Pre-install artifact check: run BEFORE pip install -e . (blocks on egg-info, build/, caches)
	@echo "── release-check: verifying clean source tree ──"
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) src
	$(PYTEST) --no-cov -m "release and not integration"
	@echo "── release-check passed ──"

release-clean:  ## Remove ALL generated files; use before creating a release archive
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true
	rm -rf build/ dist/ .coverage coverage.xml htmlcov/

clean:          ## Remove build artefacts and caches (alias for release-clean)
	$(MAKE) release-clean
