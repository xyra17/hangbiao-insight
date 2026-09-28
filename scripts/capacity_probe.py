#!/usr/bin/env python3
"""Record the S7 capacity test plan and optionally execute an HTTP probe.

The default dry-run produces a reproducible plan.  It must be run against a
real pre-production dataset for an acceptance result; this helper never labels
generated rows as Amazon evidence.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--asin-count", type=int, default=1000)
    parser.add_argument("--record-count", type=int, default=100000)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    plan = {
        "asin_count": args.asin_count,
        "record_count": args.record_count,
        "concurrency": args.concurrency,
        "p95_ms": None,
        "queue_backlog": None,
        "error_rate": None,
        "resource_peak": None,
        "status": "planned" if args.dry_run else "not_run",
    }
    if not args.dry_run:
        started = time.monotonic()
        with urllib.request.urlopen(f"{args.base_url.rstrip('/')}/api/v1/health/", timeout=10) as response:
            plan["health_status"] = response.status
        plan["health_latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        plan["status"] = "health_probe_only"
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
