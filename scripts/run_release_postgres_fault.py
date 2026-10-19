"""Restore a nonempty run dump to a disposable PostgreSQL and interrupt only it."""
import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore-result", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result_path = args.restore_result.resolve()
    output = args.output.resolve()
    if not result_path.is_relative_to(ROOT / ".local/acceptance-artifacts") or not output.is_relative_to(ROOT / ".local/acceptance-artifacts"):
        parser.error("inputs and output must belong to local release artifacts")
    if json.loads(result_path.read_text())["status"] != "passed":
        parser.error("successful nonempty joint restore result required")
    dump = result_path.parent / "database.dump"
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = "insight-release-pg-fault-" + uuid.uuid4().hex[:12]
    pgport, apiport = port(), port()
    password = secrets.token_urlsafe(32)
    env = {**os.environ, "POSTGRES_PASSWORD": password}
    container, api = None, None
    events = []

    def docker(*command, **kwargs):
        result = subprocess.run(["docker", *command], env=env, check=False, capture_output=True, **kwargs)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace")[-1500:])
        return result

    def ready():
        for _ in range(100):
            probe = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "insight", "-d", "insight_release_fault"], capture_output=True, check=False)
            if probe.returncode == 0:
                return
            time.sleep(0.2)
        raise RuntimeError("isolated PostgreSQL unavailable")

    try:
        container = docker("run", "--detach", "--name", name, "--publish", f"127.0.0.1:{pgport}:5432", "--env", "POSTGRES_PASSWORD", "--env", "POSTGRES_USER=insight", "--env", "POSTGRES_DB=insight_release_fault", "postgres:18").stdout.decode().strip()
        for _ in range(100):
            if b"PostgreSQL init process complete" in docker("logs", name).stdout:
                break
            time.sleep(0.2)
        ready()
        with dump.open("rb") as stream:
            docker("exec", "-i", name, "pg_restore", "-U", "insight", "-d", "insight_release_fault", "--no-owner", "--exit-on-error", stdin=stream)
        app_env = {**os.environ, "PGHOST": "127.0.0.1", "PGPORT": str(pgport), "PGDATABASE": "insight_release_fault", "PGUSER": "insight", "INSIGHT_DB_PASSWORD": password, "DJANGO_DEBUG": "1", "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost", "INSIGHT_STORAGE_ROOT": str(result_path.parent / "restored-storage")}
        with (output / "api.log").open("w") as log:
            api = subprocess.Popen(["uv", "run", "--directory", str(ROOT / "backend"), "--env-file", "../.env.local", "python", "manage.py", "runserver", "--noreload", f"127.0.0.1:{apiport}"], env=app_env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            base = f"http://127.0.0.1:{apiport}/api/v1"
            session = requests.Session()
            for _ in range(100):
                try:
                    if session.get(base+"/health/", timeout=1).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.2)
            accounts = json.loads(args.credentials_file.read_text())
            accounts = accounts if isinstance(accounts, list) else accounts.get("accounts", [accounts])
            admin = next(a for a in accounts if a["username"] == "accept_admin")
            csrf = session.get(base+"/session/", timeout=10).json()["csrf_token"]
            login = session.post(base+"/login/", json={"username": admin["username"], "password": admin["password"]}, headers={"X-CSRFToken": csrf}, timeout=10)
            assert login.status_code == 200
            before = session.get(base+"/snapshots/", timeout=10)
            assert before.status_code == 200 and before.json()["count"] > 0
            snapshot_id = before.json()["results"][0]["id"]
            reference = session.get(base+f"/snapshots/{snapshot_id}/", timeout=10).json()
            events.append({"operation": "before_fault", "status": 200, "snapshot_id": snapshot_id, "nonempty_count": before.json()["count"]})
            def sql(text, database="insight_release_fault"):
                return docker("exec", name, "psql", "-U", "insight", "-d", database, "-At", "-c", text).stdout.decode().strip()

            sql("CREATE TABLE release_drill_markers (sequence integer PRIMARY KEY, marked_at timestamptz NOT NULL); INSERT INTO release_drill_markers VALUES (1, clock_timestamp())")
            backup_marker = float(sql("SELECT extract(epoch FROM marked_at) FROM release_drill_markers WHERE sequence=1"))
            recovery_dump = output / "marked-backup.dump"
            recovery_dump.write_bytes(docker("exec", name, "pg_dump", "-U", "insight", "-d", "insight_release_fault", "--format=custom").stdout)
            recovery_dump.chmod(0o600)
            sql("INSERT INTO release_drill_markers VALUES (2, clock_timestamp())")
            fault_epoch = float(sql("SELECT extract(epoch FROM clock_timestamp())"))
            started = time.monotonic()
            docker("stop", "--time", "1", name)
            outage = session.get(base+f"/snapshots/{snapshot_id}/", timeout=10)
            assert outage.status_code >= 500
            events.append({"operation": "owned_postgres_stopped", "http_status": outage.status_code})
            docker("start", name)
            ready()
            after = session.get(base+f"/snapshots/{snapshot_id}/", timeout=10)
            assert after.status_code == 200 and after.json() == reference
            events.append({"operation": "business_api_recovered", "http_status": 200, "snapshot_unchanged": True, "elapsed_from_fault_seconds": round(time.monotonic()-started, 3)})
            docker("exec", name, "createdb", "-U", "insight", "insight_release_recovered")
            with recovery_dump.open("rb") as stream:
                docker("exec", "-i", name, "pg_restore", "-U", "insight", "-d", "insight_release_recovered", "--no-owner", "--exit-on-error", stdin=stream)
            restored_sequences = json.loads(sql("SELECT json_agg(sequence ORDER BY sequence) FROM release_drill_markers", "insight_release_recovered"))
            assert restored_sequences == [1]
            os.killpg(api.pid, signal.SIGTERM)
            api.wait(timeout=10)
            app_env["PGDATABASE"] = "insight_release_recovered"
            api = subprocess.Popen(["uv", "run", "--directory", str(ROOT / "backend"), "--env-file", "../.env.local", "python", "manage.py", "runserver", "--noreload", f"127.0.0.1:{apiport}"], env=app_env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            for _ in range(100):
                try:
                    restored_http = session.get(base+f"/snapshots/{snapshot_id}/", timeout=1)
                    if restored_http.status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.2)
            assert restored_http.status_code == 200 and restored_http.json() == reference
            events.append({"operation": "marked_backup_restored_business_http", "http_status": 200, "restored_sequences": restored_sequences, "lost_sequences": [2], "backup_marker_epoch": backup_marker, "fault_epoch": fault_epoch, "observed_loss_window_seconds": round(fault_epoch-backup_marker, 6), "business_rto_from_fault_seconds": round(time.monotonic()-started, 3), "target_rpo_seconds": None, "target_rto_seconds": None, "sla_status": "blocked_external_E3"})
        report = {"status": "passed", "scope": "nonempty restored clone, real HTTP snapshot before/during/after owned PostgreSQL interruption; marker-based local RPO/business HTTP RTO measured; target SLA and all mutation fault positions separate", "events": events, "no_shared_service_stopped": True}
        (output / "result.json").write_text(json.dumps(report, indent=2)+"\n")
    finally:
        if api:
            os.killpg(api.pid, signal.SIGTERM)
            try:
                api.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(api.pid, signal.SIGKILL)
                api.wait()
        if container:
            docker("rm", "--force", container)
        (output / "cleanup.json").write_text(json.dumps({"owned_api_stopped": not api or api.poll() is not None, "owned_postgres_removed": bool(container), "shared_services_untouched": True})+"\n")


if __name__ == "__main__":
    main()
