#!/usr/bin/env python3
"""Run the local 10-worker API scan/query capacity check for the S7 fixture."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psutil

ROOT = Path(__file__).resolve().parent
DEFAULT_PACK = ROOT / "data" / "08_S7容量评测_v1"
DEFAULT_OUTPUT = DEFAULT_PACK / "validation" / "capacity_run_v2.json"
POLL_INTERVAL_SECONDS = 0.5
JOB_TIMEOUT_SECONDS = 600


def local_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("API URL must be an absolute http(s) URL.")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("This runner only accepts loopback API URLs; it never sends fixture data externally.")
    return value.rstrip("/")


def pack_asins(pack_dir: Path, shop_slot: str, limit: int | None = None) -> list[str]:
    path = pack_dir / "import_ready" / shop_slot / "sales_traffic_v1.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        asins = []
        seen = set()
        for row in csv.DictReader(handle):
            asin = row["child_asin"]
            if asin not in seen:
                seen.add(asin)
                asins.append(asin)
                if limit is not None and len(asins) == limit:
                    break
    if limit is not None and len(asins) != limit:
        raise ValueError(f"Expected {limit} ASINs in {path}, found {len(asins)}.")
    return asins


def percentile_95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 3)


def percentile_50(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(0.50 * len(ordered)) - 1)], 3)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def load_accounts(credential_file: Path | None, username: str | None, password: str | None) -> list[dict[str, str]]:
    account_class = "unspecified"
    if credential_file:
        payload = json.loads(credential_file.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
            records = payload["accounts"]
            account_class = str(payload.get("account_class", account_class))
        else:
            records = [payload]
    elif username and password:
        records = [{"username": username, "password": password}]
    else:
        raise ValueError("Provide an API credential file or one username/password pair.")
    accounts = []
    for item in records:
        if not isinstance(item, dict) or not item.get("username") or not item.get("password"):
            continue
        account = {"username": str(item["username"]), "password": str(item["password"])}
        account["account_class"] = account_class
        if "shop_ids" in item:
            shop_ids = item["shop_ids"]
            if not isinstance(shop_ids, list) or any(not isinstance(shop_id, str) for shop_id in shop_ids):
                raise ValueError("Each account shop_ids value must be a list of shop IDs.")
            account["shop_ids"] = shop_ids
        accounts.append(account)
    usernames = [account["username"] for account in accounts]
    if len(accounts) != len(records) or len(set(usernames)) != len(usernames):
        raise ValueError("Credential file must contain unique usernames with nonempty passwords.")
    return accounts


def new_authenticated_client(base_url: str, username: str, password: str, *, transport=None) -> httpx.Client:
    client = httpx.Client(base_url=base_url, timeout=30, transport=transport)
    session_response = client.get("/api/v1/session/")
    session_response.raise_for_status()
    csrf = session_response.json()["csrf_token"]
    login_response = client.post(
        "/api/v1/login/",
        json={"username": username, "password": password},
        headers={"X-CSRFToken": csrf},
    )
    login_response.raise_for_status()
    client.headers["X-CSRFToken"] = login_response.json()["csrf_token"]
    return client


@contextmanager
def authenticated_client(base_url: str, username: str, password: str):
    client = new_authenticated_client(base_url, username, password)
    try:
        yield client
    finally:
        client.close()


def scan_with_client(client: httpx.Client, shop_id: str, asin: str, index: int) -> dict:
    started = time.perf_counter()
    response = client.post(
        "/api/v1/snapshots/scan/",
        json={
            "shop": shop_id,
            "child_asin": asin,
            "idempotency_key": f"s7-capacity-{os.getpid()}-{index}-{time.time_ns()}",
        },
    )
    response.raise_for_status()
    job_id = response.json()["id"]
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    job = None
    first_running_heartbeat = None
    while time.monotonic() < deadline:
        job_response = client.get(f"/api/v1/jobs/{job_id}/")
        job_response.raise_for_status()
        job = job_response.json()
        if job["status"] == "running" and job.get("heartbeat_at"):
            first_running_heartbeat = first_running_heartbeat or job["heartbeat_at"]
        if job["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    enqueued_to_terminal_ms = (time.perf_counter() - started) * 1000
    if not job or job["status"] != "completed":
        return {
            "index": index,
            "shop": shop_id,
            "asin": asin,
            "job_id": job_id,
            "snapshot_id": None,
            "enqueued_to_terminal_ms": round(enqueued_to_terminal_ms, 3),
            "snapshot_list_ms": None,
            "job_queue_wait_ms": None,
            "job_worker_runtime_ms": None,
            "request_success": True,
            "job_status": job.get("status") if job else "timeout",
            "correctness_status": "not_checked",
        }

    query_started = time.perf_counter()
    query_response = client.get(
        "/api/v1/snapshots/",
        params={"shop": shop_id, "child_asin": asin},
    )
    query_response.raise_for_status()
    query_p95_sample_ms = (time.perf_counter() - query_started) * 1000
    snapshot_id = (job.get("result") or {}).get("snapshot_id")
    matching = [item for item in query_response.json().get("results", []) if item.get("id") == snapshot_id]
    correct = bool(matching and matching[0].get("payload", {}).get("daily", {}).get("status") == "ready")
    created_at = parse_timestamp(job.get("created_at"))
    started_at = parse_timestamp(job.get("started_at") or first_running_heartbeat)
    finished_at = parse_timestamp(job.get("finished_at"))
    return {
        "index": index,
        "shop": shop_id,
        "asin": asin,
        "job_id": job_id,
        "snapshot_id": snapshot_id,
        "enqueued_to_terminal_ms": round(enqueued_to_terminal_ms, 3),
        "snapshot_list_ms": round(query_p95_sample_ms, 3),
        "job_queue_wait_ms": round((started_at - created_at).total_seconds() * 1000, 3)
        if created_at and started_at
        else None,
        "job_worker_runtime_ms": round((finished_at - started_at).total_seconds() * 1000, 3)
        if finished_at and started_at
        else None,
        "job_timing_status": "observed" if created_at and started_at and finished_at else "start_not_recorded",
        "request_success": True,
        "job_status": job["status"],
        "correctness_status": "passed" if correct else "failed",
    }


def run_scan(base_url: str, username: str, password: str, shop_id: str, asin: str, index: int) -> dict:
    with authenticated_client(base_url, username, password) as client:
        return scan_with_client(client, shop_id, asin, index)


def run_scan_batch(base_url: str, account: dict[str, str], targets: list[tuple[int, str, str]]) -> tuple[list[dict], list[dict]]:
    results = []
    failures = []
    with authenticated_client(base_url, account["username"], account["password"]) as client:
        for index, shop, asin in targets:
            try:
                results.append(scan_with_client(client, shop, asin, index))
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                failures.append(failure_record(shop, asin, exc))
    return results, failures


def api_rows(payload: dict) -> list[dict]:
    for key in ("results", "items"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def is_expected_permission_denial(endpoint: str, http_status: int | None, account: dict[str, str]) -> bool:
    """A scoped synthetic operator is expected to be denied the admin-only source registry."""
    return (
        endpoint == "sources"
        and http_status == 403
        and account.get("account_class") == "synthetic_isolated_capacity_load"
    )


def sample_business_apis(
    base_url: str,
    accounts: list[dict[str, str]],
    shop_ids: list[str],
    pack_dir: Path,
    evidence_query: str = "quality issue",
) -> dict:
    """Sample populated business APIs with distinct authenticated users in parallel."""
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    start = manifest["coverage_window"]["start"]
    end = manifest["coverage_window"]["end"]
    endpoints = {
        "events": "/api/v1/events/",
        "actions": "/api/v1/action-tasks/",
        "investigations": "/api/v1/investigations/",
        "reports": "/api/v1/reports/",
        "snapshots": "/api/v1/snapshots/",
        "sources": "/api/v1/sources/",
        "products": "/api/v1/products/",
        "jobs": "/api/v1/jobs/",
    }

    def sample_for_account(ordinal: int, account: dict[str, str]) -> list[dict]:
        declared_scope = account.get("shop_ids")
        eligible_shop_ids = [
            shop_id for shop_id in shop_ids if "shop_ids" not in account or shop_id in declared_scope
        ]
        if not eligible_shop_ids:
            return [{
                "endpoint": "authorization_scope",
                "account_index": ordinal + 1,
                "ok": False,
                "error_type": "no_authorized_sample_shop",
            }]
        shop_id = eligible_shop_ids[ordinal % len(eligible_shop_ids)]
        samples = []
        with authenticated_client(base_url, account["username"], account["password"]) as client:
            collections = {}
            for name, path in endpoints.items():
                started = time.perf_counter()
                try:
                    response = client.get(path, params={"shop": shop_id})
                    response.raise_for_status()
                    payload = response.json()
                    rows = api_rows(payload)
                    collections[name] = rows
                    samples.append({
                        "endpoint": name,
                        "account_index": ordinal + 1,
                        "shop_id": shop_id,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "http_status": response.status_code,
                        "row_count": payload.get("count", len(rows)),
                        "ok": True,
                    })
                except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    http_status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    samples.append({
                        "endpoint": name,
                        "account_index": ordinal + 1,
                        "shop_id": shop_id,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "http_status": http_status,
                        "row_count": 0,
                        "ok": False,
                        "expected_permission_denial": is_expected_permission_denial(name, http_status, account),
                        "error_type": type(exc).__name__,
                    })

            detail_endpoints = {
                "event_detail": ("events", "/api/v1/events/{id}/"),
                "event_evidence": ("events", "/api/v1/events/{id}/evidence/"),
                "action_detail": ("actions", "/api/v1/action-tasks/{id}/"),
                "investigation_detail": ("investigations", "/api/v1/investigations/{id}/"),
                "report_detail": ("reports", "/api/v1/reports/{id}/"),
                "snapshot_detail": ("snapshots", "/api/v1/snapshots/{id}/"),
            }
            for name, (collection, path_template) in detail_endpoints.items():
                rows = collections.get(collection, [])
                if not rows or not rows[0].get("id"):
                    continue
                started = time.perf_counter()
                try:
                    response = client.get(path_template.format(id=rows[0]["id"]))
                    response.raise_for_status()
                    samples.append({
                        "endpoint": name,
                        "account_index": ordinal + 1,
                        "shop_id": shop_id,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "http_status": response.status_code,
                        "row_count": 1,
                        "ok": True,
                    })
                except httpx.HTTPError as exc:
                    samples.append({
                        "endpoint": name,
                        "account_index": ordinal + 1,
                        "shop_id": shop_id,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "http_status": exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None,
                        "row_count": 0,
                        "ok": False,
                        "error_type": type(exc).__name__,
                    })

            products = collections.get("products", [])
            if products:
                product = products[0]
                started = time.perf_counter()
                try:
                    response = client.post(
                        "/api/v1/evidence-search/",
                        json={
                            "shop": shop_id,
                            "child_asin": product["child_asin"],
                            "product": product["id"],
                            "query": evidence_query,
                            "start": start,
                            "end": end,
                            "kind": "support",
                            "limit": 10,
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    samples.append({
                        "endpoint": "evidence_search",
                        "account_index": ordinal + 1,
                        "shop_id": shop_id,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "http_status": response.status_code,
                        "row_count": len(payload.get("items", [])),
                        "ok": True,
                    })
                except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    samples.append({
                        "endpoint": "evidence_search",
                        "account_index": ordinal + 1,
                        "shop_id": shop_id,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "http_status": exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None,
                        "row_count": 0,
                        "ok": False,
                        "error_type": type(exc).__name__,
                    })
        return samples

    samples = []
    if not shop_ids:
        return {"status": "failed", "reason": "no_shop_ids", "endpoints": {}}
    with ThreadPoolExecutor(max_workers=min(10, len(accounts)), thread_name_prefix="s7-api-probe") as executor:
        futures = {
            executor.submit(sample_for_account, index, account): index
            for index, account in enumerate(accounts)
        }
        for future in as_completed(futures):
            try:
                samples.extend(future.result())
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                samples.append({
                    "endpoint": "authentication",
                    "account_index": futures[future] + 1,
                    "ok": False,
                    "error_type": type(exc).__name__,
                })

    endpoint_names = sorted({sample["endpoint"] for sample in samples})
    endpoint_stats = {}
    for name in endpoint_names:
        items = [sample for sample in samples if sample["endpoint"] == name]
        latencies = [sample["latency_ms"] for sample in items if "latency_ms" in sample]
        row_counts = [sample.get("row_count", 0) for sample in items]
        endpoint_stats[name] = {
            "sample_count": len(items),
            "request_success_count": sum(sample.get("ok", False) for sample in items),
            "expected_permission_denial_count": sum(sample.get("expected_permission_denial", False) for sample in items),
            "unexpected_failure_count": sum(
                not sample.get("ok", False) and not sample.get("expected_permission_denial", False)
                for sample in items
            ),
            "nonempty_sample_count": sum(count > 0 for count in row_counts),
            "account_indices": sorted({sample["account_index"] for sample in items if "account_index" in sample}),
            "shop_ids": sorted({sample["shop_id"] for sample in items if "shop_id" in sample}),
            "latency_ms": {
                "p50": percentile_50(latencies),
                "p95": percentile_95(latencies),
                "max": round(max(latencies), 3) if latencies else None,
            },
            "row_counts": row_counts,
            "failures": [
                {key: sample[key] for key in ("http_status", "error_type") if key in sample}
                for sample in items
                if not sample.get("ok") and not sample.get("expected_permission_denial", False)
            ],
        }
    required_populated = {"events", "actions", "investigations", "reports", "snapshots", "evidence_search"}
    has_populated_data = all(
        endpoint_stats.get(name, {}).get("nonempty_sample_count", 0) > 0 for name in required_populated
    )
    requests_ok = bool(samples) and all(
        sample.get("ok") or sample.get("expected_permission_denial", False) for sample in samples
    )
    http_requests_passed = bool(samples) and all(sample.get("ok") for sample in samples)
    expected_denial_count = sum(sample.get("expected_permission_denial", False) for sample in samples)
    account_count = len({account["username"] for account in accounts})
    status = (
        "failed"
        if not requests_ok
        else "passed"
        if has_populated_data and account_count >= 10
        else "partial"
    )
    return {
        "status": status,
        "distinct_account_count": account_count,
        "concurrent_account_count": min(10, len(accounts)),
        "request_success": (
            "passed"
            if http_requests_passed
            else "expected_authorization_denials_only"
            if requests_ok and expected_denial_count
            else "failed"
        ),
        "expected_permission_denial_count": expected_denial_count,
        "population_check": "passed" if has_populated_data else "incomplete_required_interfaces_not_all_populated",
        "required_populated_endpoints": sorted(required_populated),
        "endpoint_stats": endpoint_stats,
        "limitations": [
            "Ten distinct authorized accounts are required for the 10-user sample.",
            "Events, actions, investigations, reports, snapshots, and evidence search must contain representative rows.",
            "Successful empty-list responses are recorded but do not count as representative business-interface samples.",
            "A 403 for the admin-only source registry is an expected authorization outcome for explicitly scoped synthetic operator accounts; it remains visible separately from 2xx request counts.",
        ],
    }


def active_database_backlog(shop_ids: list[str]) -> int:
    backend_root = ROOT.parent / "backend"
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    from django.apps import apps

    if not apps.ready:
        django.setup()
    from django.db import close_old_connections, connections

    from insight.models import Job

    close_old_connections()
    try:
        return Job.objects.filter(
            shop_id__in=shop_ids,
            status__in=["queued", "running", "retry_wait"],
        ).count()
    finally:
        connections.close_all()


def metric_snapshot_inventory(shop_ids: list[str], asins: list[str], snapshot_ids: set[str] | None = None) -> list[dict]:
    """Read minimal snapshot identifiers/version metadata from the local acceptance database."""
    backend_root = ROOT.parent / "backend"
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    from django.apps import apps

    if not apps.ready:
        django.setup()
    from django.db import close_old_connections, connections

    from insight.models import MetricSnapshot

    close_old_connections()
    try:
        rows = MetricSnapshot.objects.filter(shop_id__in=shop_ids, child_asin__in=asins)
        if snapshot_ids is None:
            rows = rows.filter(payload__contains={"mode": "live"})
        else:
            rows = rows.filter(pk__in=snapshot_ids)
        return [
            {"snapshot_id": str(snapshot_id), "rule_version": version, "local_date": local_date.isoformat()}
            for snapshot_id, version, local_date in rows.values_list("pk", "rule_version", "local_date")
        ]
    finally:
        connections.close_all()


def failure_record(shop: str, asin: str, exc: Exception) -> dict:
    failure = {"shop": shop, "asin": asin, "error_type": type(exc).__name__}
    if isinstance(exc, httpx.HTTPStatusError):
        failure["http_status"] = exc.response.status_code
        failure["request_path"] = exc.request.url.path
    return failure


def process_component(command: str) -> str | None:
    lowered = command.lower()
    if "celery" in lowered and "worker" in lowered:
        return "worker"
    if "celery" in lowered and "beat" in lowered:
        return "beat"
    if "manage.py" in lowered and "runserver" in lowered or "gunicorn" in lowered:
        return "api"
    if "postgres" in lowered:
        return "database"
    if "redis-server" in lowered:
        return "redis"
    return None


def parse_percent(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip().rstrip("%"))
    except (AttributeError, ValueError):
        return None


def parse_memory_mb(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*", value)
    if not match:
        return None
    amount, unit = float(match.group(1)), match.group(2).lower()
    multipliers = {
        "b": 1 / (1024**2),
        "kb": 1000 / (1024**2),
        "kib": 1 / 1024,
        "mb": 1_000_000 / (1024**2),
        "mib": 1,
        "gb": 1_000_000_000 / (1024**2),
        "gib": 1024,
        "tb": 1_000_000_000_000 / (1024**2),
        "tib": 1024**2,
    }
    multiplier = multipliers.get(unit)
    return round(amount * multiplier, 3) if multiplier is not None else None


def docker_component(container_name: str) -> str | None:
    lowered = container_name.lower()
    if "postgres" in lowered or "database" in lowered or "db-" in lowered:
        return "database"
    if "redis" in lowered or "broker" in lowered:
        return "redis"
    if "worker" in lowered or "celery" in lowered:
        return "worker"
    if "api" in lowered or "web" in lowered:
        return "api"
    if "beat" in lowered:
        return "beat"
    return None


def sample_process_resources() -> dict[str, dict]:
    grouped = {}
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            command = " ".join(process.info.get("cmdline") or [])
            component = process_component(command)
            if not component or process.pid == os.getpid():
                continue
            cpu_pct = process.cpu_percent(interval=None)
            memory_mb = process.memory_info().rss / (1024**2)
            try:
                address_space_limit = process.rlimit(psutil.RLIMIT_AS)[1]
                if address_space_limit in {-1, psutil.RLIM_INFINITY, 0}:
                    address_space_limit = None
                else:
                    address_space_limit = round(address_space_limit / (1024**2), 3)
            except (AttributeError, OSError, psutil.Error):
                address_space_limit = None
            item = grouped.setdefault(
                component,
                {"cpu_pct": 0.0, "memory_mb": 0.0, "process_count": 0, "address_space_limits_mb": []},
            )
            item["cpu_pct"] += cpu_pct
            item["memory_mb"] += memory_mb
            item["process_count"] += 1
            if address_space_limit is not None:
                item["address_space_limits_mb"].append(address_space_limit)
        except (OSError, psutil.Error):
            continue
    return grouped


def compose_container_ids(project_name: str) -> tuple[list[str], str | None]:
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={project_name}",
                "--format",
                "{{.ID}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return [], type(exc).__name__
    if result.returncode:
        return [], "DockerProjectContainersUnavailable"
    return [line.strip() for line in result.stdout.splitlines() if line.strip()], None


def sample_docker_resources(project_name: str | None = None) -> tuple[dict[str, dict], str | None]:
    container_ids = None
    if project_name:
        container_ids, error = compose_container_ids(project_name)
        if error:
            return {}, error
        if not container_ids:
            return {}, "DockerProjectHasNoRunningContainers"
    try:
        completed = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", *(container_ids or [])],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return {}, type(exc).__name__
    if completed.returncode:
        return {}, "DockerStatsUnavailable"
    grouped = {}
    for line in completed.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        component = docker_component(str(item.get("Name", "")))
        if not component:
            continue
        memory = str(item.get("MemUsage", "")).split("/", maxsplit=1)
        cpu_pct = parse_percent(item.get("CPUPerc"))
        memory_mb = parse_memory_mb(memory[0]) if memory else None
        limit_mb = parse_memory_mb(memory[1]) if len(memory) == 2 else None
        if cpu_pct is None and memory_mb is None:
            continue
        current = grouped.setdefault(
            component,
            {"cpu_pct": 0.0, "memory_mb": 0.0, "effective_memory_limit_mb": 0.0, "container_count": 0},
        )
        if cpu_pct is not None:
            current["cpu_pct"] += cpu_pct
        if memory_mb is not None:
            current["memory_mb"] += memory_mb
        if limit_mb is not None:
            current["effective_memory_limit_mb"] += limit_mb
        current["container_count"] += 1
    return grouped, None


def inspect_docker_runtime(project_name: str | None = None) -> dict:
    profile = {"status": "unavailable", "cpu_count": None, "memory_bytes": None, "container_limits": {}}
    try:
        info = subprocess.run(
            ["docker", "info", "--format", "{{.NCPU}}|{{.MemTotal}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if info.returncode == 0:
            parts = info.stdout.strip().split("|", maxsplit=1)
            if len(parts) == 2:
                profile["cpu_count"] = int(parts[0])
                profile["memory_bytes"] = int(parts[1])
                profile["status"] = "observed"

        command = ["docker", "ps"]
        if project_name:
            command.extend(["--filter", f"label=com.docker.compose.project={project_name}"])
        command.extend(["--format", "{{.ID}}|{{.Names}}"])
        listing = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        containers = [
            (parts[0], docker_component(parts[1]))
            for line in listing.stdout.splitlines()
            if len(parts := line.split("|", maxsplit=1)) == 2 and docker_component(parts[1])
        ]
        if listing.returncode or not containers:
            return profile
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.HostConfig.NanoCpus}}|{{.HostConfig.CpuQuota}}|{{.HostConfig.CpuPeriod}}|{{.HostConfig.Memory}}",
                *(container_id for container_id, _component in containers),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if inspected.returncode:
            return profile
        by_component: dict[str, dict] = {}
        for (_container_id, component), line in zip(containers, inspected.stdout.splitlines(), strict=False):
            parts = line.split("|")
            if len(parts) != 4:
                continue
            nano_cpus, cpu_quota, cpu_period, memory_limit = map(int, parts)
            cpu_limit = nano_cpus / 1_000_000_000 if nano_cpus > 0 else (
                cpu_quota / cpu_period if cpu_quota > 0 and cpu_period > 0 else None
            )
            item = by_component.setdefault(
                component,
                {
                    "container_count": 0,
                    "configured_cpu_limit_cores": 0.0,
                    "configured_memory_limit_mb": 0.0,
                    "unlimited_cpu_container_count": 0,
                    "unlimited_memory_container_count": 0,
                },
            )
            item["container_count"] += 1
            if cpu_limit is None:
                item["unlimited_cpu_container_count"] += 1
            else:
                item["configured_cpu_limit_cores"] += cpu_limit
            if memory_limit <= 0:
                item["unlimited_memory_container_count"] += 1
            else:
                item["configured_memory_limit_mb"] += memory_limit / (1024**2)
        for item in by_component.values():
            if item["unlimited_cpu_container_count"]:
                item["configured_cpu_limit_cores"] = None
            else:
                item["configured_cpu_limit_cores"] = round(item["configured_cpu_limit_cores"], 3)
            if item["unlimited_memory_container_count"]:
                item["configured_memory_limit_mb"] = None
            else:
                item["configured_memory_limit_mb"] = round(item["configured_memory_limit_mb"], 3)
        profile["container_limits"] = by_component
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, ValueError):
        return profile
    return profile


def inspect_celery_workers() -> dict:
    backend_root = ROOT.parent / "backend"
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        import django
        from django.apps import apps

        if not apps.ready:
            django.setup()
        from config.celery import app

        stats = app.control.inspect(timeout=1).stats() or {}
    except Exception as exc:  # noqa: BLE001 - measurement should report missing inspection, not abort the run.
        return {"status": "unavailable", "worker_count": None, "total_concurrency": None, "error_type": type(exc).__name__}
    capacities = [
        int(info.get("pool", {}).get("max-concurrency", 0))
        for info in stats.values()
        if isinstance(info, dict) and isinstance(info.get("pool"), dict)
    ]
    return {
        "status": "observed" if stats else "no_workers_responded",
        "worker_count": len(stats),
        "total_concurrency": sum(capacities),
        "reported_pool_capacities": capacities,
    }


class RuntimeSampler:
    def __init__(self, shop_ids: list[str], stats_url: str | None, compose_project: str | None = None):
        self.shop_ids = shop_ids
        self.stats_url = stats_url
        self.compose_project = compose_project
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="s7-runtime-sampler", daemon=True)
        self.peak_backlog: int | None = None
        self.peak_cpu: float | None = None
        self.peak_memory: float | None = None
        self.backlog_error: str | None = None
        self.stats_error: str | None = None
        self.sample_count = 0
        self.resource_sample_count = 0
        self.resource_errors: set[str] = set()
        self.resource_usage: dict[str, dict] = {}
        self.last_docker_sample = 0.0
        self.docker_runtime = inspect_docker_runtime(compose_project)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=10)

    def _record_resource(self, component: str, values: dict, source: str) -> None:
        item = self.resource_usage.setdefault(
            component,
            {
                "sample_count": 0,
                "sources": set(),
                "peak_cpu_pct": None,
                "peak_memory_mb": None,
                "effective_memory_limit_mb": None,
                "process_address_space_limit_mb": None,
                "peak_process_count": 0,
                "peak_container_count": 0,
            },
        )
        item["sample_count"] += 1
        item["sources"].add(source)
        for field, source_field in (("peak_cpu_pct", "cpu_pct"), ("peak_memory_mb", "memory_mb")):
            value = values.get(source_field)
            if value is not None:
                item[field] = max(value, item[field] or 0.0)
        for field in ("effective_memory_limit_mb", "process_address_space_limit_mb"):
            value = values.get(field)
            if value is not None:
                item[field] = value if item[field] is None else max(value, item[field])
        item["peak_process_count"] = max(item["peak_process_count"], values.get("process_count", 0))
        item["peak_container_count"] = max(item["peak_container_count"], values.get("container_count", 0))

    def resource_summary(self) -> dict:
        return {
            component: {
                **{key: value for key, value in item.items() if key != "sources"},
                "sources": sorted(item["sources"]),
                "container_configured_limits": self.docker_runtime["container_limits"].get(component),
            }
            for component, item in sorted(self.resource_usage.items())
        }

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                backlog = active_database_backlog(self.shop_ids)
                self.peak_backlog = max(backlog, self.peak_backlog or 0)
            except Exception as exc:  # noqa: BLE001 - keep measurement alive and record sampler error type.
                self.backlog_error = type(exc).__name__
            for component, values in sample_process_resources().items():
                limits = values.get("address_space_limits_mb", [])
                values["process_address_space_limit_mb"] = min(limits) if limits else None
                self._record_resource(component, values, "psutil_host_process")
            now = time.monotonic()
            if now - self.last_docker_sample >= 5:
                docker_samples, error_type = sample_docker_resources(self.compose_project)
                self.last_docker_sample = now
                self.resource_sample_count += 1
                if error_type:
                    self.resource_errors.add(error_type)
                for component, values in docker_samples.items():
                    self._record_resource(component, values, "docker_stats")
            if self.stats_url:
                try:
                    with httpx.Client(timeout=5) as stats_client:
                        response = stats_client.get(self.stats_url)
                        response.raise_for_status()
                        payload = response.json()
                    cpu = payload.get("server_peak_cpu_pct")
                    memory = payload.get("server_peak_memory_mb")
                    if cpu is not None:
                        self.peak_cpu = max(float(cpu), self.peak_cpu or 0.0)
                    if memory is not None:
                        self.peak_memory = max(float(memory), self.peak_memory or 0.0)
                    if cpu is not None or memory is not None:
                        self._record_resource(
                            "api",
                            {
                                "cpu_pct": float(cpu) if cpu is not None else None,
                                "memory_mb": float(memory) if memory is not None else None,
                            },
                            "loopback_stats_endpoint",
                        )
                except (httpx.HTTPError, json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as exc:
                    self.stats_error = type(exc).__name__
            self.sample_count += 1
            self.stop_event.wait(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base-url", default=os.getenv("INSIGHT_API_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--username", default=os.getenv("INSIGHT_API_USERNAME"))
    parser.add_argument("--password", default=os.getenv("INSIGHT_API_PASSWORD"))
    parser.add_argument("--credential-file", type=Path, help="Private local JSON file written by prepare_capacity_acceptance.py.")
    parser.add_argument("--shop-a", required=True, help="Isolated shop UUID for shop_a's imported fixture.")
    parser.add_argument("--shop-b", required=True, help="Isolated shop UUID for shop_b's imported fixture.")
    parser.add_argument("--pack-dir", type=Path, default=DEFAULT_PACK)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--targets-per-shop",
        type=int,
        default=None,
        help="Optional smoke-test cap; acceptance scans all 500 ASINs per shop.",
    )
    parser.add_argument(
        "--sample-shop",
        action="append",
        default=[],
        help="Additional isolated shop UUIDs used for business API sampling.",
    )
    parser.add_argument(
        "--evidence-query",
        default="synthetic",
        help="Search text used for the synthetic capacity corpus; each search writes its normal audit entry.",
    )
    parser.add_argument(
        "--server-stats-url",
        default=os.getenv("INSIGHT_CAPACITY_STATS_URL"),
        help="Optional loopback JSON endpoint exposing server_peak_cpu_pct and server_peak_memory_mb.",
    )
    parser.add_argument(
        "--compose-project",
        default=os.getenv("INSIGHT_COMPOSE_PROJECT"),
        help="Optional Docker Compose project name used to isolate container resource samples from unrelated stacks.",
    )
    args = parser.parse_args()
    try:
        accounts = load_accounts(args.credential_file, args.username, args.password)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    if not accounts:
        parser.error("Credential file did not contain any usable API accounts.")

    try:
        base_url = local_base_url(args.api_base_url)
        stats_url = local_base_url(args.server_stats_url) if args.server_stats_url else None
    except ValueError as exc:
        parser.error(str(exc))

    if args.targets_per_shop is not None and args.targets_per_shop < 1:
        parser.error("--targets-per-shop must be positive when supplied.")
    assignments = [
        (args.shop_a, asin)
        for asin in pack_asins(args.pack_dir, "shop_a", args.targets_per_shop)
    ] + [
        (args.shop_b, asin)
        for asin in pack_asins(args.pack_dir, "shop_b", args.targets_per_shop)
    ]
    expected_targets = 1000 if args.targets_per_shop is None else args.targets_per_shop * 2
    if len(assignments) != expected_targets:
        parser.error("The S7 contract requires a full 1,000-ASIN scan unless an explicit smoke-test cap is supplied.")

    target_asins = sorted({asin for _shop_id, asin in assignments})
    baseline_snapshots = metric_snapshot_inventory([args.shop_a, args.shop_b], target_asins)
    baseline_snapshot_ids = {row["snapshot_id"] for row in baseline_snapshots}
    results = []
    failures = []
    run_started = time.perf_counter()
    sampler = RuntimeSampler([args.shop_a, args.shop_b], stats_url, args.compose_project)
    sampler.start()
    indexed_assignments = [(index, shop, asin) for index, (shop, asin) in enumerate(assignments, start=1)]
    account_targets: dict[int, list[tuple[int, str, str]]] = {}
    shop_account_cursors: dict[str, int] = {}
    for target in indexed_assignments:
        _index, shop_id, asin = target
        eligible_accounts = [
            index
            for index, account in enumerate(accounts)
            if "shop_ids" not in account or shop_id in account["shop_ids"]
        ]
        if not eligible_accounts:
            failures.append({"shop": shop_id, "asin": asin, "error_type": "NoAuthorizedAccount"})
            continue
        cursor = shop_account_cursors.get(shop_id, 0)
        account_index = eligible_accounts[cursor % len(eligible_accounts)]
        shop_account_cursors[shop_id] = cursor + 1
        account_targets.setdefault(account_index, []).append(target)

    worker_count = min(10, len(account_targets))
    scan_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, worker_count), thread_name_prefix="s7-worker") as executor:
        futures = {
            executor.submit(run_scan_batch, base_url, accounts[index], targets): targets
            for index, targets in account_targets.items()
        }
        for future in as_completed(futures):
            try:
                batch_results, batch_failures = future.result()
                results.extend(batch_results)
                failures.extend(batch_failures)
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                # Capture expected target failures without credentials or response bodies.
                failures.extend(failure_record(shop, asin, exc) for _index, shop, asin in futures[future])
    scan_wall_ms = (time.perf_counter() - scan_started) * 1000
    sample_shop_ids = list(dict.fromkeys([args.shop_a, args.shop_b, *args.sample_shop]))
    api_sampling_started = time.perf_counter()
    api_samples = sample_business_apis(
        base_url, accounts, sample_shop_ids, args.pack_dir, evidence_query=args.evidence_query
    )
    api_sampling_wall_ms = (time.perf_counter() - api_sampling_started) * 1000
    sampler.stop()
    celery_runtime = inspect_celery_workers()
    wall_total_ms = (time.perf_counter() - run_started) * 1000

    returned_snapshot_ids = {str(result["snapshot_id"]) for result in results if result.get("snapshot_id")}
    returned_snapshot_rows = metric_snapshot_inventory(
        [args.shop_a, args.shop_b], target_asins, returned_snapshot_ids
    ) if returned_snapshot_ids else []
    new_snapshot_ids = returned_snapshot_ids - baseline_snapshot_ids
    snapshot_generation = {
        "baseline_live_snapshot_count": len(baseline_snapshot_ids),
        "returned_unique_snapshot_count": len(returned_snapshot_ids),
        "new_snapshot_count": len(new_snapshot_ids),
        "cached_result_count": sum(
            str(result.get("snapshot_id")) in baseline_snapshot_ids
            for result in results
            if result.get("snapshot_id")
        ),
        "snapshot_inventory_status": "observed",
        "rule_version_counts": {},
        "local_date_counts": {},
    }
    for row in returned_snapshot_rows:
        snapshot_generation["rule_version_counts"][row["rule_version"]] = (
            snapshot_generation["rule_version_counts"].get(row["rule_version"], 0) + 1
        )
        snapshot_generation["local_date_counts"][row["local_date"]] = (
            snapshot_generation["local_date_counts"].get(row["local_date"], 0) + 1
        )

    snapshot_list_times = [result["snapshot_list_ms"] for result in results if result.get("snapshot_list_ms") is not None]
    enqueued_times = [result["enqueued_to_terminal_ms"] for result in results]
    request_success_count = sum(result.get("request_success", False) for result in results)
    job_success_count = sum(result.get("job_status") == "completed" for result in results)
    correctness_pass_count = sum(result.get("correctness_status") == "passed" for result in results)
    request_status = "passed" if request_success_count == len(assignments) and not failures else "failed"
    correctness_status = "passed" if correctness_pass_count == len(assignments) and job_success_count == len(assignments) else "failed"
    account_count = len({account["username"] for account in accounts})
    api_endpoint_stats = api_samples.get("endpoint_stats", {})
    required_latency_endpoints = {"events", "actions", "investigations", "reports", "snapshots", "evidence_search"}
    api_latency_samples = [
        stats["latency_ms"]["p95"]
        for name, stats in api_endpoint_stats.items()
        if name in required_latency_endpoints
        and stats.get("latency_ms", {}).get("p95") is not None
    ]
    required_api_samples_complete = (
        api_samples.get("status") == "passed"
        and required_latency_endpoints.issubset(api_endpoint_stats)
        and len(api_latency_samples) == len(required_latency_endpoints)
        and bool(snapshot_list_times)
    )
    snapshot_p95 = percentile_95(snapshot_list_times)
    measured_api_slow = any(value > 3000 for value in api_latency_samples) or (
        snapshot_p95 is not None and snapshot_p95 > 3000
    )
    api_performance_status = (
        "failed"
        if measured_api_slow
        else "passed_local_smoke_thresholds"
        if required_api_samples_complete
        else "not_evaluated_incomplete_samples"
    )
    full_capacity_run = args.targets_per_shop is None and len(assignments) == 1000
    if not full_capacity_run:
        performance_status = "not_evaluated_smoke_only"
    elif scan_wall_ms > 900_000 or api_performance_status == "failed":
        performance_status = "failed"
    elif required_api_samples_complete and scan_wall_ms <= 900_000:
        performance_status = "passed_local_full_run_only_target_environment_pending"
    else:
        performance_status = "not_evaluated_incomplete_samples"
    sampling_status = api_samples.get("status", "failed")
    target_environment_status = "pending_external_8_core_16_gb_ssd_environment"
    job_completion_status = "passed" if job_success_count == len(assignments) else "failed"
    execution_status = "passed" if request_status == "passed" and job_completion_status == "passed" else "failed"
    all_target_error_count = len(assignments) - request_success_count
    resource_summary = sampler.resource_summary()
    required_resource_components = {"api", "worker", "database", "redis"}
    observed_resource_components = {
        component
        for component, item in resource_summary.items()
        if item.get("peak_cpu_pct") is not None and item.get("peak_memory_mb") is not None
    }
    resource_observability_status = (
        "sampled_all_required_components"
        if required_resource_components.issubset(observed_resource_components)
        else "partial"
        if observed_resource_components
        else "not_measured"
    )
    os_memory_bytes = psutil.virtual_memory().total
    docker_engine = sampler.docker_runtime
    measurement = {
        "status": execution_status,
        "status_semantics": "Status means every requested scan was accepted and its job completed; correctness, API sampling, performance, resource observation, and target environment gates are separate.",
        "acceptance_status": {
            "requests": request_status,
            "job_completion": job_completion_status,
            "correctness": correctness_status,
            "sampling": sampling_status,
            "api_query_performance": api_performance_status,
            "performance": performance_status,
            "resource_observability": resource_observability_status,
            "target_environment": target_environment_status,
        },
        "environment": "loopback_only",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "worker_count": worker_count,
        "scan_client_worker_count": worker_count,
        "configured_worker_limit": 10,
        "celery_worker_runtime": celery_runtime,
        "distinct_authenticated_account_count": account_count,
        "account_classes": sorted({account["account_class"] for account in accounts}),
        "authenticated_account_status": "10_distinct_users" if account_count >= 10 else "insufficient_distinct_users",
        "host_profile": {
            "platform": platform.platform(),
            "cpu_count": psutil.cpu_count(),
            "memory_bytes": os_memory_bytes,
            "disk_type": "not_measured",
            "docker_runtime": docker_engine,
        },
        "target_count": len(assignments),
        "measurements": {
            "request_success_count": request_success_count,
            "job_success_count": job_success_count,
            "correctness_pass_count": correctness_pass_count,
            "request_error_count": all_target_error_count,
            "error_rate_pct": round(100 * all_target_error_count / len(assignments), 3),
            "snapshot_list_sample_ms": {
                "sample_count": len(snapshot_list_times),
                "p50": percentile_50(snapshot_list_times),
                "p95": percentile_95(snapshot_list_times),
                "max": round(max(snapshot_list_times), 3) if snapshot_list_times else None,
            },
            "query_p95_ms": percentile_95(snapshot_list_times),
            "api_endpoint_p95_ms": {
                name: stats["latency_ms"]["p95"]
                for name, stats in sorted(api_endpoint_stats.items())
                if stats.get("latency_ms", {}).get("p95") is not None
            },
            "enqueued_to_terminal_ms": {
                "sample_count": len(enqueued_times),
                "p50": percentile_50(enqueued_times),
                "p95": percentile_95(enqueued_times),
                "max": round(max(enqueued_times), 3) if enqueued_times else None,
            },
            "scan_total_ms": round(max(enqueued_times), 3) if enqueued_times else None,
            "longest_target_elapsed_ms": round(max(enqueued_times), 3) if enqueued_times else None,
            "scan_concurrent_wall_ms": round(scan_wall_ms, 3),
            "api_sampling_wall_ms": round(api_sampling_wall_ms, 3),
            "full_measurement_wall_ms": round(wall_total_ms, 3),
            "concurrent_wall_ms": round(wall_total_ms, 3),
            "index_build_ms": None,
            "index_build_status": "not_measured_by_capacity_runner",
            "snapshot_generation": snapshot_generation,
            "job_queue_wait_ms": {
                "sample_count": sum(result.get("job_queue_wait_ms") is not None for result in results),
                "p50": percentile_50([result["job_queue_wait_ms"] for result in results if result.get("job_queue_wait_ms") is not None]),
                "p95": percentile_95([result["job_queue_wait_ms"] for result in results if result.get("job_queue_wait_ms") is not None]),
            },
            "job_worker_runtime_ms": {
                "sample_count": sum(result.get("job_worker_runtime_ms") is not None for result in results),
                "p50": percentile_50([result["job_worker_runtime_ms"] for result in results if result.get("job_worker_runtime_ms") is not None]),
                "p95": percentile_95([result["job_worker_runtime_ms"] for result in results if result.get("job_worker_runtime_ms") is not None]),
            },
            "queue_backlog": sampler.peak_backlog,
            "queue_backlog_source": "local_database_active_jobs",
            "queue_backlog_sample_count": sampler.sample_count,
            "queue_backlog_error": sampler.backlog_error,
            "server_peak_cpu_pct": sampler.peak_cpu,
            "server_peak_memory_mb": sampler.peak_memory,
            "server_stats_status": "sampled" if stats_url and sampler.sample_count else "not_configured" if not stats_url else f"unavailable:{sampler.stats_error or 'no_samples'}",
            "runtime_sample_count": sampler.sample_count,
            "resource_sample_count": sampler.resource_sample_count,
            "resource_usage_by_component": resource_summary,
            "resource_errors": sorted(sampler.resource_errors),
        },
        "business_api_samples": api_samples,
        "target_environment_status": target_environment_status,
        "completed": sorted(results, key=lambda item: item["index"]),
        "failures": failures,
        "limitations": [
            "Requires the S7 fixture to be imported into the two supplied isolated shops before running.",
            "The default run scans all 1,000 imported ASINs with a maximum of 10 concurrent scan clients; client workers and Celery worker concurrency are reported separately.",
            "Ten scan client threads are not ten users unless the credential file contains ten distinct authorized accounts.",
            "API sampling records list/detail and evidence-search latency separately and marks empty fixtures as insufficient samples; evidence search writes a normal audit entry.",
            "scan_total_ms is retained as a legacy alias for the longest target elapsed time; scan_concurrent_wall_ms measures the batch and excludes API sampling.",
            "Queue backlog is the maximum local-database count of queued, running, and retry_wait jobs sampled once per second.",
            "Index build time is not measured by this runner; it must come from a real index build/import measurement and is never inferred from scan time.",
            "Host processes are sampled with psutil once per second; Docker stats sample containers every five seconds. Process and container configured/effective limits are reported separately where available.",
            "A smoke-test cap never evaluates the 1,000-target, 900-second scan gate; local measurements do not constitute target-environment or production capacity acceptance.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(measurement, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in measurement.items() if key != "completed"}, ensure_ascii=False, indent=2))
    print(f"Result: {args.output}")
    return 0 if measurement["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
