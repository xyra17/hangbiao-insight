"""Profile a metric scan and semantic index rebuild on a disposable acceptance clone."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Command failed ({result.returncode}): {command[0]} {command[1]}\n{detail[-3000:]}")
    return result


def main() -> int:
    run_id = uuid.uuid4().hex
    clone_database = f"insight_profile_{run_id[:12]}"
    output_dir = ROOT / ".local" / "profile-drills" / run_id
    clone_storage = output_dir / "storage"
    os.umask(0o077)
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    source_storage = ROOT / ".local" / "acceptance-storage"
    if not source_storage.is_dir():
        raise RuntimeError("The local synthetic acceptance storage fixture is unavailable.")
    shutil.copytree(source_storage, clone_storage)
    compose = ["docker", "compose", "--env-file", ".env.local", "-f", "compose.dev.yml", "exec", "-T", "postgres"]
    created = False
    record = {
        "status": "running",
        "profile_scope": "one scan against a disposable clone of insight_acceptance",
        "source_database": "insight_acceptance",
        "clone_database": clone_database,
        "as_of_policy": "uses the current site date; no clock override",
    }
    try:
        run(compose + ["createdb", "-U", os.getenv("PGUSER", "insight"), "-T", "insight_acceptance", clone_database])
        created = True
        code = r'''
import cProfile, json, pstats, time
import django
django.setup()
from django.contrib.auth import get_user_model
from django.db import connection
from insight.metrics.service import scan_object
from insight.models import Product

actor = get_user_model().objects.filter(is_active=True, membership__is_admin=True).order_by("username").first()
if actor is None:
    raise RuntimeError("Synthetic acceptance administrator is missing from the clone.")
product = Product.objects.filter(
    child_asin="B000TEST03",
    shop__workspace__membership__user=actor,
).select_related("shop").order_by("id").first()
if product is None:
    raise RuntimeError("The populated B000TEST03 metric fixture is unavailable in the cloned database.")
shop = product.shop

queries = {}
query_time = 0.0
def measure(execute, sql, params, many, context):
    global query_time
    started = time.perf_counter()
    try:
        return execute(sql, params, many, context)
    finally:
        elapsed = time.perf_counter() - started
        query_time += elapsed
        key = __import__("hashlib").sha256(" ".join(sql.split()).encode()).hexdigest()[:16]
        row = queries.setdefault(key, {"fingerprint": key, "count": 0, "total_ms": 0.0, "max_ms": 0.0})
        row["count"] += 1
        row["total_ms"] += elapsed * 1000
        row["max_ms"] = max(row["max_ms"], elapsed * 1000)

profiler = cProfile.Profile()
started = time.perf_counter()
with connection.execute_wrapper(measure):
    profiler.enable()
    try:
        snapshot = scan_object(actor, shop, product.child_asin)
    finally:
        profiler.disable()
wall_ms = (time.perf_counter() - started) * 1000
daily_status = snapshot.payload["daily"]["status"]
if daily_status != "ready":
    raise RuntimeError(f"B000TEST03 must have complete daily metric facts; got status={daily_status!r}.")

stats = pstats.Stats(profiler).sort_stats("cumulative")
function_rows = []
for (filename, line, name), (calls, primitive, total, cumulative, callers) in stats.stats.items():
    function_rows.append({"function": f"{filename}:{line}:{name}", "calls": calls, "cumulative_ms": round(cumulative * 1000, 3), "self_ms": round(total * 1000, 3)})
function_rows.sort(key=lambda row: row["cumulative_ms"], reverse=True)
print(json.dumps({
  "status": "passed",
  "shop_id": str(shop.pk),
  "product_asin": product.child_asin,
  "fixture": "B000TEST03 ASIN grain with a published 35-day complete synthetic sales/traffic report",
  "snapshot_id": str(snapshot.pk),
  "daily_status": daily_status,
  "wall_ms": round(wall_ms, 3),
  "database_execute_ms": round(query_time * 1000, 3),
  "database_query_count": sum(row["count"] for row in queries.values()),
  "top_query_fingerprints": sorted(queries.values(), key=lambda row: row["total_ms"], reverse=True)[:15],
  "top_functions": function_rows[:30],
}, ensure_ascii=False))
'''
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "PGDATABASE": clone_database,
            "PGHOST": "127.0.0.1",
            "PGPORT": os.getenv("PGPORT", "55432"),
            "PGUSER": os.getenv("PGUSER", "insight"),
            "INSIGHT_STORAGE_ROOT": str(clone_storage),
        }
        profile = run(
            ["uv", "run", "--directory", str(BACKEND), "--env-file", "../.env.local", "python", "-c", code],
            env=env,
        )
        measured = json.loads(profile.stdout)
        index_started = time.perf_counter()
        index_command = run(
            [
                "uv", "run", "--directory", str(BACKEND), "--env-file", "../.env.local",
                "python", "manage.py", "rebuild_evidence_index",
            ],
            env=env,
        )
        index_wall_ms = round((time.perf_counter() - index_started) * 1000, 3)
        index_verify = run(
            [
                "uv", "run", "--directory", str(BACKEND), "--env-file", "../.env.local",
                "python", "-c",
                "import django, json; django.setup(); "
                "from insight.models import Review, SupportCase; "
                "from insight.semantic.indexing import INDEX_VERSION; "
                "print(json.dumps({m.__name__: {'total': m.objects.count(), "
                "'indexed': m.objects.filter(search_index_version=INDEX_VERSION, search_vector__isnull=False).count()} "
                "for m in (Review, SupportCase)}))",
            ],
            env=env,
        )
        index_counts = json.loads(index_verify.stdout)
        if any(item["total"] != item["indexed"] for item in index_counts.values()):
            raise RuntimeError(f"Semantic index rebuild left rows unindexed: {index_counts}")
        record.update(measured)
        record["semantic_index_build"] = {
            "status": "passed",
            "entrypoint": "manage.py rebuild_evidence_index",
            "wall_ms": index_wall_ms,
            "index_version": "jieba-0.42.1-feedback-v1",
            "stdout": index_command.stdout.strip(),
            "post_build_rows": index_counts,
            "scope": "all review and support facts in the disposable acceptance database clone",
        }
        record["limitations"] = [
            "The disposable source is the small synthetic B000TEST03 metric fixture, not the 502,400-row S7 capacity package.",
            "The measured index build covers only the existing small synthetic review/support corpus; it is not a capacity measurement or a substitute for the full labelled-corpus build.",
            "The metric profile identifies representative code/query hotspots; it is not a capacity measurement or a substitute for a full scan.",
            "SQL text and bound data are not stored; only stable query fingerprints and timings are retained.",
        ]
        record["status"] = "passed"
    except Exception as exc:
        record.update({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)})
    finally:
        (output_dir / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if created:
            run(compose + ["dropdb", "-U", os.getenv("PGUSER", "insight"), clone_database])
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
