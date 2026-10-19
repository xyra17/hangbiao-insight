"""Profile one S7 capacity scan in its isolated test workspace without recording SQL parameters."""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import os
import pstats
import sys
import time
from pathlib import Path

import django
from django.db import connection

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
VALIDATION_ROOT = REPOSITORY_ROOT / "dataset" / "data" / "08_S7容量评测_v1" / "validation"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shop-id", required=True)
    parser.add_argument("--asin", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = args.output.resolve()
    if not output.is_relative_to(VALIDATION_ROOT.resolve()):
        parser.error("Profile reports must be written under the S7 capacity validation directory.")
    sys.path.insert(0, str(BACKEND_ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from django.contrib.auth import get_user_model

    from insight.metrics.service import scan_object
    from insight.models import Product, Shop

    shop = Shop.objects.select_related("workspace").get(pk=args.shop_id)
    if not shop.workspace.is_test or not shop.workspace.name.startswith("s7-capacity-v1.1"):
        parser.error("Refusing to profile outside the isolated S7 capacity workspace.")
    actor = (
        get_user_model()
        .objects.filter(is_active=True, membership__workspace=shop.workspace, membership__is_admin=True)
        .order_by("username")
        .first()
    )
    product = Product.objects.filter(shop=shop, child_asin=args.asin).select_related("shop").first()
    if actor is None or product is None:
        parser.error("The isolated S7 capacity administrator or requested product is missing.")

    sql_rows: dict[str, dict[str, float | int | str]] = {}
    database_execute_seconds = 0.0

    def measure(execute, sql, params, many, context):
        nonlocal database_execute_seconds
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            elapsed = time.perf_counter() - started
            database_execute_seconds += elapsed
            fingerprint = hashlib.sha256(" ".join(sql.split()).encode()).hexdigest()[:16]
            row = sql_rows.setdefault(
                fingerprint,
                {"fingerprint": fingerprint, "count": 0, "total_ms": 0.0, "max_ms": 0.0},
            )
            row["count"] += 1
            row["total_ms"] += elapsed * 1000
            row["max_ms"] = max(row["max_ms"], elapsed * 1000)

    profiler = cProfile.Profile()
    started = time.perf_counter()
    with connection.execute_wrapper(measure):
        profiler.enable()
        try:
            snapshot = scan_object(actor, shop, product.child_asin, record_events=False)
        finally:
            profiler.disable()
    wall_ms = (time.perf_counter() - started) * 1000
    daily_status = snapshot.payload.get("daily", {}).get("status")

    functions = []
    for (filename, line, name), (calls, primitive_calls, total, cumulative, _callers) in pstats.Stats(
        profiler
    ).sort_stats("cumulative").stats.items():
        functions.append(
            {
                "function": f"{filename}:{line}:{name}",
                "calls": calls,
                "cumulative_ms": round(cumulative * 1000, 3),
                "self_ms": round(total * 1000, 3),
            }
        )
    functions.sort(key=lambda row: row["cumulative_ms"], reverse=True)
    record = {
        "status": "passed" if daily_status == "ready" else "failed",
        "scope": "one historical profile scan in the isolated S7 capacity workspace; event side effects are disabled",
        "shop_id": str(shop.pk),
        "asin": product.child_asin,
        "snapshot_id": str(snapshot.pk),
        "daily_status": daily_status,
        "calculation_mode": snapshot.payload.get("mode"),
        "wall_ms": round(wall_ms, 3),
        "database_execute_ms": round(database_execute_seconds * 1000, 3),
        "database_query_count": sum(row["count"] for row in sql_rows.values()),
        "top_query_fingerprints": sorted(sql_rows.values(), key=lambda row: row["total_ms"], reverse=True)[:20],
        "top_functions": functions[:40],
        "limitations": [
            "One representative target does not predict the 1,000-target wall time.",
            "SQL parameters, raw text, and review/customer data are not recorded.",
            "The scan creates a normal immutable metric snapshot in the isolated test workspace.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    print(f"Result: {output}")
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
