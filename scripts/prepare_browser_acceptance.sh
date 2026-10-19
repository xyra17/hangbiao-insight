#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
usage="Usage: ACCEPTANCE_ACCOUNTS_FILE=.local/acceptance-accounts-<run>.json $0 [as-of YYYY-MM-DD]"
accounts_file=${ACCEPTANCE_ACCOUNTS_FILE:-}
if [ -z "$accounts_file" ]; then
  echo "$usage" >&2
  exit 2
fi
if [ "$#" -gt 1 ]; then
  echo "$usage" >&2
  exit 2
fi
as_of=${1:-}
case "$accounts_file" in
  /*) accounts_file=$(cd -- "$(dirname -- "$accounts_file")" && pwd)/$(basename -- "$accounts_file") ;;
  *) accounts_file="$repo_root/$accounts_file" ;;
esac
case "$accounts_file" in
  "$repo_root"/.local/*) ;;
  *) echo "Acceptance credentials must stay under the repository .local directory." >&2; exit 2 ;;
esac
if [ ! -f "$repo_root/.env.local" ]; then
  echo "Missing local credentials/config file: $repo_root/.env.local" >&2
  exit 2
fi

export ACCEPTANCE_ACCOUNTS_FILE="$accounts_file"
acceptance_db=${ACCEPTANCE_DATABASE:-insight_acceptance}
case "$acceptance_db" in
  insight_acceptance|insight_acceptance_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]|insight_acceptance_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_v[1-9]*) ;;
  *) echo "Acceptance database must be insight_acceptance or insight_acceptance_YYYYMMDD." >&2; exit 2 ;;
esac
if ! printf '%s\n' "$acceptance_db" | rg -q '^insight_acceptance(_[0-9]{8}(_v[1-9][0-9]*)?)?$'; then
  echo "Invalid acceptance database suffix" >&2; exit 2
fi
export PGDATABASE="$acceptance_db"
export PGHOST=127.0.0.1
export PGPORT=55432
export PGUSER=insight
export DJANGO_DEBUG=1
export INSIGHT_STORAGE_ROOT="$repo_root/.local/acceptance-storage/$acceptance_db"
export INSIGHT_ACCEPTANCE_ENV="$acceptance_db"
export INSIGHT_CELERY_NAMESPACE="$acceptance_db"
artifact_dir=${ACCEPTANCE_ARTIFACT_DIR:-"$repo_root/.local/acceptance-artifacts/$(date -u +%Y%m%dT%H%M%SZ)_prepare_$acceptance_db"}
mkdir -p "$artifact_dir"
export INSIGHT_BEAT_SCHEDULE="$artifact_dir/beat-${acceptance_db}.db"
umask 077
lock_dir="$repo_root/.local/acceptance-locks/$acceptance_db"
mkdir -p "$(dirname -- "$lock_dir")"
if [ -n "${ACCEPTANCE_RUNNER_LOCK_PID:-}" ]; then
  if [ ! -f "$lock_dir/pid" ] || [ "$(cat "$lock_dir/pid")" != "$ACCEPTANCE_RUNNER_LOCK_PID" ] || ! kill -0 "$ACCEPTANCE_RUNNER_LOCK_PID" 2>/dev/null; then
    echo "Acceptance environment lock does not belong to the active runner." >&2
    exit 2
  fi
else
  if ! mkdir "$lock_dir" 2>/dev/null; then
    echo "Acceptance environment is locked; refusing concurrent seed writes." >&2
    exit 2
  fi
  printf '%s\n' "$$" >"$lock_dir/pid"
  trap 'rm -f "$lock_dir/pid"; rmdir "$lock_dir" 2>/dev/null || true' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
fi

cd "$repo_root"
docker compose -f compose.dev.yml --env-file .env.local up -d --wait postgres redis
database_exists=$(docker compose -f compose.dev.yml --env-file .env.local exec -T postgres \
  psql -U insight -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$acceptance_db'")
if [ "$database_exists" != "1" ]; then
  docker compose -f compose.dev.yml --env-file .env.local exec -T postgres \
    createdb -U insight "$acceptance_db"
fi

cd "$repo_root/backend"
uv run --env-file ../.env.local python manage.py check_acceptance_environment
uv run --env-file ../.env.local python manage.py migrate --noinput
uv run --env-file ../.env.local python manage.py seed_acceptance --credentials-output "$ACCEPTANCE_ACCOUNTS_FILE"
uv run --env-file ../.env.local python manage.py seed_metric_acceptance
if [ -n "$as_of" ]; then
  uv run --env-file ../.env.local python manage.py seed_acceptance_browser_fixtures --as-of "$as_of"
else
  uv run --env-file ../.env.local python manage.py seed_acceptance_browser_fixtures
fi
uv run --env-file ../.env.local python manage.py seed_investigation_evidence_acceptance --as-of "${as_of:-$(TZ=America/Los_Angeles date +%F)}"
uv run --env-file ../.env.local python manage.py seed_metric_evidence_acceptance --as-of "${as_of:-$(TZ=America/Los_Angeles date +%F)}"
if [ ! -s "$ACCEPTANCE_ACCOUNTS_FILE" ]; then
  echo "The isolated DB already contains accounts; set ACCEPTANCE_ACCOUNTS_FILE to their existing local credentials file." >&2
  exit 2
fi
echo "Browser acceptance fixtures prepared in database $acceptance_db. Credentials remain local."
