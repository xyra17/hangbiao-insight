#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
accounts_file=${ACCEPTANCE_ACCOUNTS_FILE:-}
if [[ -z "$accounts_file" ]]; then
  echo "Set ACCEPTANCE_ACCOUNTS_FILE to a private .local credentials file." >&2
  exit 2
fi
case "$accounts_file" in
  /*) ;;
  *) accounts_file="$repo_root/$accounts_file" ;;
esac
accounts_file="$(cd -- "$(dirname -- "$accounts_file")" && pwd)/$(basename -- "$accounts_file")"
case "$accounts_file" in
  "$repo_root"/.local/*) ;;
  *) echo "Acceptance credentials must stay under the repository .local directory." >&2; exit 2 ;;
esac

as_of=""
if [[ $# -gt 0 && "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  as_of="$1"
  shift
fi
playwright_args=("$@")
if [[ -z "$as_of" ]]; then
  as_of=$(TZ=America/Los_Angeles date +%F)
fi
export ACCEPTANCE_AS_OF="$as_of"
version=${ACCEPTANCE_RUN_VERSION:-2}
if [[ ! "$version" =~ ^[1-9][0-9]*$ ]]; then echo "Invalid acceptance run version" >&2; exit 2; fi
acceptance_db="insight_acceptance_${as_of//-/}_v${version}"
export ACCEPTANCE_DATABASE="$acceptance_db"

for port in 8280 5184; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "Port $port is already in use; refusing to reuse an unknown process." >&2
    exit 2
  fi
done

run_id=$(date -u +%Y%m%dT%H%M%SZ)
artifact_dir="$repo_root/.local/acceptance-artifacts/${run_id}_${acceptance_db}"
mkdir -p "$artifact_dir"
chmod 700 "$repo_root/.local/acceptance-artifacts" "$artifact_dir"
export ACCEPTANCE_SCREENSHOT_DIR="$artifact_dir/screenshots"
mkdir -p "$ACCEPTANCE_SCREENSHOT_DIR"
chmod 700 "$ACCEPTANCE_SCREENSHOT_DIR"

export ACCEPTANCE_ARTIFACT_DIR="$artifact_dir"
export INSIGHT_ACCEPTANCE_ENV="$acceptance_db"
export INSIGHT_CELERY_NAMESPACE="$acceptance_db"
export INSIGHT_BEAT_SCHEDULE="$artifact_dir/beat-${acceptance_db}.db"
export INSIGHT_STORAGE_ROOT="$repo_root/.local/acceptance-storage/$acceptance_db"
export PGDATABASE="$acceptance_db" PGHOST=127.0.0.1 PGPORT=55432 PGUSER=insight DJANGO_DEBUG=1
lock_dir="$repo_root/.local/acceptance-locks/$acceptance_db"
mkdir -p "$(dirname -- "$lock_dir")"
if ! mkdir "$lock_dir" 2>/dev/null; then
  echo "Acceptance environment $acceptance_db is locked; refusing duplicate startup." >&2
  exit 2
fi
printf '%s\n' "$$" >"$lock_dir/pid"
export ACCEPTANCE_RUNNER_LOCK_PID="$$"
pids=()
cleanup() {
  local pid
  local survivors=0
  for pid in "${pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  for pid in "${pids[@]:-}"; do
    wait "$pid" 2>/dev/null || true
    if kill -0 "$pid" 2>/dev/null; then survivors=$((survivors + 1)); fi
  done
  printf '{"owned_service_survivors":%s,"environment":"%s"}\n' "$survivors" "$acceptance_db" >"$artifact_dir/cleanup.json"
  rm -f "$lock_dir/pid"
  rmdir "$lock_dir" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
sh "$repo_root/scripts/prepare_browser_acceptance.sh" "$as_of" >"$artifact_dir/prepare.log" 2>&1

(
  cd "$repo_root/backend"
  PGDATABASE="$acceptance_db" PGHOST=127.0.0.1 PGPORT=55432 PGUSER=insight \
    DJANGO_DEBUG=1 INSIGHT_LOGIN_THROTTLE_ACCOUNT_LIMIT=100 \
    INSIGHT_STORAGE_ROOT="$repo_root/.local/acceptance-storage/$acceptance_db" \
    exec python3 "$repo_root/scripts/acceptance_process.py" uv run --env-file ../.env.local python manage.py runserver 127.0.0.1:8280 --noreload
) >"$artifact_dir/api.log" 2>&1 &
pids+=("$!")

(
  cd "$repo_root/backend"
  PGDATABASE="$acceptance_db" PGHOST=127.0.0.1 PGPORT=55432 PGUSER=insight \
    DJANGO_DEBUG=1 INSIGHT_STORAGE_ROOT="$repo_root/.local/acceptance-storage/$acceptance_db" \
    exec python3 "$repo_root/scripts/acceptance_process.py" uv run --env-file ../.env.local celery -A config.celery worker -Q "$acceptance_db" --hostname "${acceptance_db}@%h" --pool=solo --concurrency=1 --loglevel=INFO
) >"$artifact_dir/worker.log" 2>&1 &
pids+=("$!")

(
  cd "$repo_root/backend"
  PGDATABASE="$acceptance_db" PGHOST=127.0.0.1 PGPORT=55432 PGUSER=insight \
    DJANGO_DEBUG=1 INSIGHT_STORAGE_ROOT="$repo_root/.local/acceptance-storage/$acceptance_db" \
    exec python3 "$repo_root/scripts/acceptance_process.py" uv run --env-file ../.env.local celery -A config.celery beat --schedule "$INSIGHT_BEAT_SCHEDULE" --loglevel=INFO
) >"$artifact_dir/beat.log" 2>&1 &
pids+=("$!")

(
  cd "$repo_root/frontend"
  exec python3 "$repo_root/scripts/acceptance_process.py" npm run dev -- --host 127.0.0.1
) >"$artifact_dir/vite.log" 2>&1 &
pids+=("$!")

ready=0
worker_ready=0
beat_ready=0
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8280/api/v1/health/ >/dev/null 2>&1 \
    && curl -fsS http://127.0.0.1:5184/ >/dev/null 2>&1; then
    ready=1
  fi
  if rg -q 'ready\.' "$artifact_dir/worker.log" 2>/dev/null; then worker_ready=1; fi
  if rg -q 'beat: Starting' "$artifact_dir/beat.log" 2>/dev/null; then beat_ready=1; fi
  if [[ "$ready" == 1 && "$worker_ready" == 1 && "$beat_ready" == 1 ]]; then break; fi
  sleep 1
done
if [[ "$ready" != 1 || "$worker_ready" != 1 || "$beat_ready" != 1 ]]; then
  echo "Local API, worker, beat, or Vite health check failed. Logs: $artifact_dir" >&2
  exit 1
fi

cd "$repo_root/frontend"
if [[ ${#playwright_args[@]} -eq 0 ]]; then
  ACCEPTANCE_ACCOUNTS_FILE="$accounts_file" ACCEPTANCE_SCREENSHOT_DIR="$ACCEPTANCE_SCREENSHOT_DIR" \
    npm run test:e2e -- --reporter=list 2>&1 | tee "$artifact_dir/playwright.log"
else
  ACCEPTANCE_ACCOUNTS_FILE="$accounts_file" ACCEPTANCE_SCREENSHOT_DIR="$ACCEPTANCE_SCREENSHOT_DIR" \
    npm run test:e2e -- --reporter=list "${playwright_args[@]}" 2>&1 | tee "$artifact_dir/playwright.log"
fi

echo "Browser acceptance artifacts: $artifact_dir"
