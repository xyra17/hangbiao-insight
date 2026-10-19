.PHONY: install dev dev-infra dev-infra-down backend worker beat frontend migrate test test-capacity planner-comparison test-legacy scripts-test frontend-test build verify lint check migrations openapi business-acceptance

DEV_PROJECT := insight-local-dev
DEV_DB := insight_dev
DEV_PGPORT := 55433
DEV_REDIS_PORT := 56380
DEV_NAMESPACE := insight_dev
DEV_ENV = PGDATABASE=$(DEV_DB) PGHOST=127.0.0.1 PGPORT=$(DEV_PGPORT) PGUSER=insight DJANGO_DEBUG=1 INSIGHT_REDIS_HOST=127.0.0.1 INSIGHT_REDIS_PORT=$(DEV_REDIS_PORT) INSIGHT_CELERY_NAMESPACE=$(DEV_NAMESPACE) INSIGHT_STORAGE_ROOT=../.local/dev/storage INSIGHT_BEAT_SCHEDULE=../.local/dev/celerybeat.db INVESTIGATION_PLANNER_PROVIDER=deterministic INVESTIGATION_EVAL_PROVIDER=deterministic EVIDENCE_RELEVANCE_ENABLED=0 EVIDENCE_RELEVANCE_PROVIDER= SEMANTIC_PROVIDER=disabled INSIGHT_ENABLED_MODEL_PROVIDERS=
TEST_ENV = PGDATABASE=$(DEV_DB) PGHOST=127.0.0.1 PGPORT=$(DEV_PGPORT) PGUSER=insight DJANGO_DEBUG=1 INSIGHT_REDIS_HOST=127.0.0.1 INSIGHT_REDIS_PORT=$(DEV_REDIS_PORT) INSIGHT_CELERY_NAMESPACE=insight_test INSIGHT_STORAGE_ROOT=../.local/test/storage INSIGHT_BEAT_SCHEDULE=../.local/test/celerybeat.db INVESTIGATION_PLANNER_PROVIDER=deterministic INVESTIGATION_EVAL_PROVIDER=deterministic EVIDENCE_RELEVANCE_ENABLED=0 EVIDENCE_RELEVANCE_PROVIDER= SEMANTIC_PROVIDER=disabled INSIGHT_ENABLED_MODEL_PROVIDERS=
DELIVERY_RUN ?= $(shell date -u +%Y%m%dT%H%M%SZ)_internal-delivery
DELIVERY_AS_OF ?= 2026-10-01
TEST_EVAL_SUFFIX ?= local_$(shell date -u +%Y%m%dT%H%M%SZ)_$(shell python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')

install:
	cd backend && uv sync --dev --frozen
	cd frontend && pnpm install --frozen-lockfile

# A separately named Compose project with its own named volumes and host ports.
# It does not recreate or stop the existing insight-v2 development services.
dev-infra:
	@test -f .env.local || { echo 'Create .env.local from backend/.env.example and fill the local Django/PostgreSQL/Redis settings first.' >&2; exit 2; }
	PGDATABASE=$(DEV_DB) PGPORT=$(DEV_PGPORT) REDIS_PORT=$(DEV_REDIS_PORT) docker compose -p $(DEV_PROJECT) -f compose.v2.yml --env-file .env.local up -d --wait postgres redis

dev-infra-down:
	docker compose -p $(DEV_PROJECT) -f compose.v2.yml --env-file .env.local down

migrate: dev-infra
	cd backend && $(DEV_ENV) uv run --env-file ../.env.local python manage.py migrate --noinput

check: dev-infra
	cd backend && $(DEV_ENV) uv run --env-file ../.env.local python manage.py check

migrations: dev-infra
	cd backend && $(DEV_ENV) uv run --env-file ../.env.local python manage.py makemigrations --check --dry-run

openapi: dev-infra
	cd backend && $(DEV_ENV) uv run --env-file ../.env.local python manage.py spectacular --file /tmp/insight-openapi.yaml --validate

lint:
	cd backend && uv run --env-file ../.env.local ruff check config insight tests_v2

dev: migrate
	$(DEV_ENV) python3 scripts/supervise_dev.py --repo-root . --ready-port 8280 --ready-port 5184

backend: migrate
	cd backend && $(DEV_ENV) uv run --env-file ../.env.local python manage.py runserver 127.0.0.1:8280 --noreload

worker: dev-infra
	cd backend && $(DEV_ENV) uv run --env-file ../.env.local celery -A config worker --loglevel=INFO --concurrency=2

beat: dev-infra
	cd backend && $(DEV_ENV) uv run --env-file ../.env.local celery -A config beat --loglevel=INFO

frontend:
	cd frontend && pnpm dev -- --host 127.0.0.1 --strictPort

# The Django runner creates and drops test_insight_dev; the development DB is not used for test data.
test: dev-infra
	cd backend && INVESTIGATION_EVAL_REPORT_SUFFIX=$(TEST_EVAL_SUFFIX) $(TEST_ENV) uv run --env-file ../.env.local pytest -m 'not capacity'

# Same 48 synthetic tasks, source facts, max-step budget and scorer across the
# fixed two-read workflow and deterministic planner. This target is pinned to zero
# provider dispatch; external-model comparison requires a separate authorized run.
planner-comparison: dev-infra
	cd backend && INVESTIGATION_EVAL_REPORT_SUFFIX=$(TEST_EVAL_SUFFIX) $(TEST_ENV) uv run --env-file ../.env.local pytest -c pytest-v2.ini tests_v2/test_s4_s6_runtime_acceptance.py tests_v2/test_core_execution_contract.py::test_model_planner_cannot_dispatch_from_allowlist_without_approval tests_v2/test_model_cost_readiness.py::test_real_factory_passes_output_limit_to_controlled_sdk_and_refuses_over_budget -q

# Deliberate opt-in: this imports and verifies 502,400 rows and is not part of verify or CI.
test-capacity: dev-infra
	cd backend && $(TEST_ENV) uv run --env-file ../.env.local pytest -m capacity

# Compatibility tests for the retained FastAPI prototype; not the default test suite.
test-legacy:
	cd backend && uv run --env-file ../.env.local pytest tests

scripts-test:
	cd backend && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -c /dev/null ../scripts/tests -q -p no:cacheprovider

frontend-test:
	cd frontend && pnpm test

build:
	cd frontend && pnpm build

verify: dev-infra lint check migrations openapi test scripts-test frontend-test build

# Runs the current 05 package through import, scan, evidence/investigation,
# 06 action/review/recurrence, complete forward action lineage, and browser acceptance on one isolated DB.
business-acceptance: dev-infra
	@set -eu; \
	  for phase in main actions forward-complete browser; do \
	    (cd backend && $(DEV_ENV) uv run --env-file ../.env.local python ../scripts/run_business_package_acceptance.py \
	      --run-dir ../.local/acceptance-artifacts/$(DELIVERY_RUN) --as-of $(DELIVERY_AS_OF) --pack 05 --phase $$phase); \
	  done
