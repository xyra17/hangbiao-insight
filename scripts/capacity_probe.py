#!/usr/bin/env python3
"""Run a bounded concurrent HTTP capacity probe and emit machine-readable evidence.

The probe measures the configured endpoint only.  It never fabricates Amazon
records or labels a health endpoint as a business-data acceptance result.  Use
the target pre-production API and an authenticated header for the S7 capacity
gate; ``--dry-run`` only validates the requested load plan.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeResult:
    latency_ms: float
    status: int | None
    error: str | None = None


def _request(url: str, timeout: float, headers: dict[str, str]) -> ProbeResult:
    started = time.perf_counter()
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(256)
            return ProbeResult((time.perf_counter() - started) * 1000, response.status)
    except Exception as exc:  # probe output must include failures, not abort at the first one
        return ProbeResult((time.perf_counter() - started) * 1000, None, type(exc).__name__)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((percentile / 100) * len(ordered))) - 1))
    return round(ordered[index], 2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--asin-count", type=int, default=1000)
    parser.add_argument("--record-count", type=int, default=100000)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--endpoint", default="/api/v1/health/")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="optional HTTP header; repeatable and never printed in the result",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.asin_count < 1 or args.record_count < 1 or args.concurrency < 1 or args.requests < 1:
        parser.error("asin-count、record-count、concurrency 和 requests 必须为正数")
    if args.timeout <= 0:
        parser.error("timeout 必须为正数")
    if not args.endpoint.startswith("/"):
        parser.error("endpoint 必须以 / 开头")
    plan = {
        "asin_count": args.asin_count,
        "record_count": args.record_count,
        "concurrency": args.concurrency,
        "requests": args.requests,
        "endpoint": args.endpoint,
        "timeout_seconds": args.timeout,
        "p95_ms": None,
        "p50_ms": None,
        "max_ms": None,
        "successful_requests": 0,
        "failed_requests": 0,
        "queue_backlog": None,
        "error_rate": None,
        "resource_peak": None,
        "status": "planned" if args.dry_run else "not_run",
    }
    if not args.dry_run:
        headers = {}
        for item in args.header:
            if "=" not in item:
                parser.error("header 必须使用 NAME=VALUE 格式")
            name, value = item.split("=", 1)
            if not name.strip():
                parser.error("header 名称不能为空")
            headers[name.strip()] = value
        url = f"{args.base_url.rstrip('/')}{args.endpoint}"
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(_request, url, args.timeout, headers) for _ in range(args.requests)]
            results = [future.result() for future in futures]
        elapsed = time.monotonic() - started
        latencies = [item.latency_ms for item in results]
        successful = [item for item in results if item.status is not None and 200 <= item.status < 400]
        failed = len(results) - len(successful)
        plan.update(
            {
                "url": url,
                "duration_seconds": round(elapsed, 3),
                "successful_requests": len(successful),
                "failed_requests": failed,
                "p50_ms": _percentile(latencies, 50),
                "p95_ms": _percentile(latencies, 95),
                "max_ms": round(max(latencies), 2) if latencies else None,
                "error_rate": round(failed / len(results), 4) if results else None,
                "status": "passed" if failed == 0 else "failed",
                "errors": sorted({item.error for item in results if item.error}),
            }
        )
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0 if plan["status"] in {"planned", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
