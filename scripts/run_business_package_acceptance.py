#!/usr/bin/env python3
"""Full local public-API package acceptance with real isolated Celery workers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg
from business_package_oracle import daily_windows, read_rows, summary
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temp.replace(path)


def checked(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        # Log only this synthetic acceptance API's error, never headers/cookies/secrets.
        raise RuntimeError(f"{method} {path}: {response.status_code} {response.text[:1000]}")
    return response.json()


def wait_job(client, job_id, timeout=900):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = checked(client, "GET", f"/api/v1/jobs/{job_id}/")
        if job["status"] in {"completed", "failed", "cancelled"}:
            if job["status"] != "completed":
                raise RuntimeError(f"Job {job_id}: {job['status']} {job.get('error')}")
            return job
        time.sleep(0.5)
    raise TimeoutError(f"Job {job_id} timed out")


def wait_batch(client, batch_id, target="preview", timeout=900):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        batch = checked(client, "GET", f"/api/v1/imports/{batch_id}/")
        if batch["status"] in {target, "published"}:
            return batch
        if batch["status"] in {"invalid", "failed"}:
            raise RuntimeError(f"Batch {batch_id}: {batch['status']} {batch.get('error')}")
        time.sleep(0.5)
    raise TimeoutError(f"Batch {batch_id} timed out")


def import_file(client, path, shop_id, state, state_path, *, source_key=None, primary=True):
    key = str(path)
    item = state["files"].setdefault(key, {})
    sidecar = json.loads(Path(str(path) + ".meta.json").read_text())
    payload = {**sidecar["source_payload"], "shop": shop_id}
    kind = payload["kind"]
    source_key = source_key or kind
    payload["is_primary"] = primary
    payload["name"] = f"Release {state['pack']} {source_key} {state['run_id']}"
    source_id = state["sources"].get(source_key)
    if not source_id:
        # Recover a create response lost before its local state write.
        listed = checked(client, "GET", "/api/v1/sources/", params={"shop": shop_id, "page_size": 100})
        found = next((s for s in listed["results"] if s["name"] == payload["name"]), None)
        source_id = found["id"] if found else checked(client, "POST", "/api/v1/sources/", json=payload)["id"]
        state["sources"][source_key] = source_id
        save(state_path, state)
    if not item.get("batch_id"):
        data = sidecar["upload_payload"]
        data = {k: str(v).lower() if isinstance(v, bool) else v for k, v in data.items()}
        with path.open("rb") as handle:
            batch = checked(client, "POST", f"/api/v1/sources/{source_id}/upload/", data=data, files={"file": (path.name, handle, "text/csv")})
        item.update(batch_id=batch["id"], file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), source_id=source_id)
        save(state_path, state)
    batch = wait_batch(client, item["batch_id"])
    if batch["error_rows"]:
        errors = checked(client, "GET", f"/api/v1/imports/{batch['id']}/rows/", params={"errors": "1"})
        save(state_path.parent / f"errors-{batch['id']}.json", errors)
        raise AssertionError(f"Main package rejected {batch['error_rows']} rows; inspect errors artifact")
    if batch["status"] != "published":
        job = checked(client, "POST", f"/api/v1/imports/{batch['id']}/publish/", json={"allow_partial": False, "confirm_contract": True})
        item["publish_job_id"] = job["id"]
        save(state_path, state)
        wait_job(client, job["id"])
        batch = checked(client, "GET", f"/api/v1/imports/{batch['id']}/")
    # Actual duplicate publish must resolve to the same durable job and no new version.
    duplicate = checked(client, "POST", f"/api/v1/imports/{batch['id']}/publish/", json={"allow_partial": False, "confirm_contract": True})
    wait_job(client, duplicate["id"])
    assert not item.get("publish_job_id") or duplicate["id"] == item["publish_job_id"]
    item.update(status=batch["status"], input_rows=batch["total_rows"], valid_rows=batch["valid_rows"], rejected_rows=batch["error_rows"], revision=batch["published_revision"], duplicate_publish_job=duplicate["id"], coverage_status=batch["coverage_status"])
    save(state_path, state)
    return batch


def launch(env, as_of, command, logfile):
    handle = logfile.open("a")
    process = subprocess.Popen([sys.executable, str(ROOT / "scripts/acceptance_frozen_process.py"), "--as-of", as_of, "--clock-file", str(logfile.parent / "business-clock.json"), "--", *command],
                               cwd=ROOT / "backend", env=env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    return process, handle


def manage(env, args, logfile):
    with logfile.open("a") as output:
        subprocess.run([sys.executable, "manage.py", *args], cwd=ROOT / "backend", env=env, stdout=output, stderr=subprocess.STDOUT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--pack", choices=["04", "05"], required=True)
    parser.add_argument("--phase", choices=["main", "lifecycle", "actions", "forward-complete", "browser", "originals"], default="main")
    parser.add_argument("--pack-dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    if not run_dir.is_relative_to(ROOT / ".local/acceptance-artifacts"):
        parser.error("run-dir must be repository .local/acceptance-artifacts")
    run_dir.mkdir(parents=True, exist_ok=True)
    output = run_dir / f"package-{args.pack}"
    output.mkdir(exist_ok=True)
    pack = (args.pack_dir or ROOT / "dataset/data" / ("04_经营事实增强" if args.pack == "04" else "05_近期经营闭环_v1")).resolve()
    if not pack.is_relative_to(ROOT / "dataset/data"):
        parser.error("Only repository local package inputs; no external transmission")
    database = f"insight_acceptance_20261007_v{1000 + int(hashlib.sha256((str(run_dir)+args.pack).encode()).hexdigest()[:5],16)}"
    assert re.fullmatch(r"insight_acceptance_[0-9]{8}_v[1-9][0-9]*", database)
    port = 8291 if args.pack == "04" else 8292
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError("Acceptance API port already occupied; refusing unknown process reuse")
    owner = output / "environment-owner.json"
    pg_host = os.getenv("PGHOST", "127.0.0.1")
    pg_port = os.getenv("PGPORT", "55433")
    if pg_host not in {"127.0.0.1", "localhost"} or pg_port != "55433":
        raise RuntimeError("Business acceptance requires the isolated local PostgreSQL at 127.0.0.1:55433")
    with psycopg.connect(host=pg_host, port=pg_port, user=os.getenv("PGUSER", "insight"), password=os.environ["INSIGHT_DB_PASSWORD"], dbname="postgres", autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", [database]).fetchone()
        if exists and not owner.exists():
            raise RuntimeError("Existing database lacks this run ownership record")
        if not exists:
            save(owner, {"database": database, "run_dir": str(run_dir), "created_at_real_utc": datetime.now(UTC).isoformat(), "pack": args.pack})
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    env = {**os.environ, "PGDATABASE": database, "PGHOST": pg_host, "PGPORT": pg_port, "DJANGO_DEBUG": "1", "DJANGO_SETTINGS_MODULE": "config.settings",
           "INSIGHT_ACCEPTANCE_ENV": database, "INSIGHT_CELERY_NAMESPACE": database,
           "INSIGHT_BEAT_SCHEDULE": str(output / f"beat-{database}.db"), "INSIGHT_STORAGE_ROOT": str(ROOT / ".local/acceptance-storage" / database),
           "INVESTIGATION_PLANNER_PROVIDER": "deterministic", "INVESTIGATION_EVAL_PROVIDER": "deterministic",
           "EVIDENCE_RELEVANCE_ENABLED": "0", "EVIDENCE_RELEVANCE_PROVIDER": "", "SEMANTIC_PROVIDER": "disabled",
           "INSIGHT_ENABLED_MODEL_PROVIDERS": "",
           "CSRF_TRUSTED_ORIGINS": f"http://127.0.0.1:{5186 if args.pack == '04' else 5188}"}
    lock = ROOT / ".local/acceptance-locks" / database
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.mkdir()  # Atomic; never reuse another run's lock.
    (lock / "pid").write_text(str(os.getpid()))
    services = []
    try:
        manage(env, ["check_acceptance_environment"], output / "prepare.log")
        manage(env, ["migrate", "--noinput"], output / "prepare.log")
        credentials = output / "accounts.json"
        manage(env, ["seed_acceptance", "--credentials-output", str(credentials)], output / "prepare.log")
        identities = output / "identities.json"
        manage(env, ["seed_release_package", "--pack", args.pack, "--pack-dir", str(pack), "--output", str(identities)], output / "prepare.log")
        identities = json.loads(identities.read_text())
        clock_path = output / "business-clock.json"
        if not clock_path.exists() or json.loads(clock_path.read_text())["as_of"] != args.as_of:
            save(clock_path, {"as_of": args.as_of, "anchor_real_utc": datetime.now(UTC).isoformat(), "scope": "isolated historical business replay; real elapsed time ticks"})
        services.append(launch(env, args.as_of, ["manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"], output / "api.log"))
        services.append(launch(env, args.as_of, ["celery", "-A", "config.celery", "worker", "-Q", database, "--hostname", f"{database}@%h", "--pool=solo", "--concurrency=1", "--loglevel=INFO"], output / "worker.log"))
        services.append(launch(env, args.as_of, ["celery", "-A", "config.celery", "beat", "--loglevel=INFO"], output / "beat.log"))
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, timeout=180, trust_env=False) as client:
            for _ in range(120):
                try:
                    if client.get("/api/v1/health/").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            session = checked(client, "GET", "/api/v1/session/")
            account = next(a for a in json.loads(credentials.read_text()) if a["username"] == "accept_admin")
            login = checked(client, "POST", "/api/v1/login/", json=account, headers={"X-CSRFToken": session["csrf_token"]})
            client.headers["X-CSRFToken"] = login["csrf_token"]
            if args.phase != "main":
                from business_package_followups import run_followups
                run_followups(args.phase, client, env, output, pack, identities, args.as_of)
                return 0
            state_path = output / "state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {"run_id": run_dir.name, "pack": args.pack, "files": {}, "sources": {}, "snapshots": {}, "database": database, "as_of": args.as_of}
            assert state["as_of"] == args.as_of
            manifest = json.loads((pack / "manifest.json").read_text())
            if args.pack == "04":
                paths = [pack / "import_ready" / name for name in manifest["files"]]
            else:
                paths = [pack / manifest["files"][kind]["path"] for kind in ["sales_traffic", "orders", "refunds", "returns", "inventory", "ratings", "operations", "reviews", "support"]]
            expected = {}
            for path in paths:
                kind = json.loads(Path(str(path) + ".meta.json").read_text())["source_payload"]["kind"]
                expected[str(path)] = summary(read_rows(path), kind)
                batch = import_file(client, path, identities["shop_id"], state, state_path)
                assert batch["total_rows"] == expected[str(path)]["rows"] and batch["valid_rows"] == batch["total_rows"]
                print(f"{path.name}: {batch['valid_rows']}/{batch['total_rows']} published revision {batch['published_revision']}", flush=True)
            save(output / "csv-oracle.json", expected)
            asins = sorted({p["child_asin"] for p in identities["products"]} & set().union(*(set(e["asins"]) for e in expected.values())))
            sales_path = next(p for p in paths if json.loads(Path(str(p)+".meta.json").read_text())["source_payload"]["kind"] == "sales_traffic")
            sales_rows = read_rows(sales_path)
            snapshots = []
            for asin in asins:
                job = checked(client, "POST", "/api/v1/snapshots/scan/", json={"shop": identities["shop_id"], "child_asin": asin, "idempotency_key": f"release-{run_dir.name}-{args.pack}-{asin}-{args.as_of}"})
                job = wait_job(client, job["id"])
                snapshot = checked(client, "GET", f"/api/v1/snapshots/{job['result']['snapshot_id']}/")
                assert snapshot["local_date"] == args.as_of
                windows = daily_windows(sales_rows, asin, args.as_of)
                daily = snapshot["payload"]["daily"]
                if args.pack == "04":
                    assert daily["status"] == "missing", daily
                else:
                    assert daily["status"] == "ready", daily
                    for window in ["baseline", "current"]:
                        for field in ["ordered_units", "order_count", "sessions", "sales_amount", "unit_session_percent"]:
                            observed, desired = daily[window][field], windows[window][field]
                            assert Decimal(str(observed)) == Decimal(desired), (asin, window, field, observed, desired)
                snapshots.append({"asin": asin, "snapshot": snapshot, "independent_daily_oracle": windows, "daily_check": "passed"})
                state["snapshots"][asin] = snapshot["id"]
                save(state_path, state)
            save(output / "snapshots-and-oracle.json", snapshots)
            # Actual authorized original-file download must byte-match the input.
            downloads = []
            for path in paths:
                batch_id = state["files"][str(path)]["batch_id"]
                download = client.get(f"/api/v1/imports/{batch_id}/original-file/")
                assert download.status_code == 200 and hashlib.sha256(download.content).hexdigest() == hashlib.sha256(path.read_bytes()).hexdigest()
                downloads.append({"batch_id": batch_id, "sha256": hashlib.sha256(download.content).hexdigest(), "status": 200})
            save(output / "original-downloads.json", downloads)
            manage(env, ["audit_release_package", "--state", str(state_path), "--output", str(output / "db-audit.json")], output / "audit.log")
            audit = json.loads((output / "db-audit.json").read_text())
            for record in audit["records"]:
                oracle = expected[record["filename"]]
                assert record["rows"] == oracle["rows"] == record["raw_row_links"]
                assert record["asins"] == oracle["asins"] and record["missing_numeric_values"] == oracle["missing_numeric_values"]
                for k, v in oracle["totals"].items():
                    assert Decimal(record["totals"][k]) == Decimal(v), (record["filename"], k)
                assert {k: Decimal(v) for k,v in record["amount_by_currency"].items()} == {k: Decimal(v) for k,v in oracle["amount_by_currency"].items()}
                assert record["file_sha256"] == state["files"][record["filename"]]["file_sha256"]
                assert all(j["status"] == "completed" and j["started_at"] for j in record["jobs"])
            result = {"schema_version": "business-package-acceptance-v1", "status": "passed", "pack": args.pack, "database": database, "as_of": args.as_of,
                      "actual_finished_at_utc": datetime.now(UTC).isoformat(), "clock": "Django business clock frozen; wall/monotonic real", "full_rows": sum(e["rows"] for e in expected.values()),
                      "main_batches": len(paths), "object_count": len(asins), "independent_oracle": "CSV Decimal; no production calculator imported", "async_worker": True,
                      "coverage_declarations_preserved": True, "scope": "full main-package import/persistence/scan/original-file and duplicate-publication; lifecycle/browser reported separately"}
            save(output / "result.json", result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        for process, handle in services:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process, handle in services:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            handle.close()
        (lock / "pid").unlink(missing_ok=True)
        lock.rmdir()
        save(output / "cleanup.json", {"owned_service_survivors": sum(p.poll() is None for p, _ in services), "database_preserved_for_browser_and_restore": database, "no_shared_service_stopped": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
