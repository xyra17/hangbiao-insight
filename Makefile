.PHONY: install dev backend frontend test build preflight local-release verify db-backup db-restore db-backup-all db-restore-all

install:
	cd backend && uv sync --dev
	cd frontend && pnpm install

dev:
	@trap 'kill 0' INT TERM EXIT; \
	(cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8200) & \
	(cd frontend && pnpm dev) & \
	wait

backend:
	cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8200

frontend:
	cd frontend && pnpm dev

test:
	cd backend && uv run pytest

build:
	cd frontend && pnpm build

preflight:
	cd backend && uv run python -m app.preflight

local-release:
	cd backend && EXECUTION_MODE=deterministic_local uv run python -m app.preflight

verify: test build

db-backup:
	cd backend && uv run python -m app.db_backup backup $(BACKUP_PATH)

db-restore:
	cd backend && uv run python -m app.db_backup restore $(BACKUP_PATH)

db-backup-all:
	cd backend && uv run python -m app.db_backup backup-all $(BACKUP_DIR)

db-restore-all:
	cd backend && uv run python -m app.db_backup restore-all $(BACKUP_DIR)
