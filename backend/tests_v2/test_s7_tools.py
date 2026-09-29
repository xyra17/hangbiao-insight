import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib handler API
        if self.path != "/api/v1/health/":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *_args):
        return


@pytest.mark.django_db
def test_capacity_probe_records_concurrent_latency_and_error_budget(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        script = Path(__file__).resolve().parents[2] / "scripts" / "capacity_probe.py"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "--requests",
                "8",
                "--concurrency",
                "3",
                "--header",
                "X-Test=probe",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        assert payload["status"] == "passed"
        assert payload["successful_requests"] == 8
        assert payload["failed_requests"] == 0
        assert payload["p95_ms"] is not None
        assert payload["error_rate"] == 0.0
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_capacity_probe_dry_run_never_contacts_endpoint():
    script = Path(__file__).resolve().parents[2] / "scripts" / "capacity_probe.py"
    result = subprocess.run(
        [sys.executable, str(script), "--base-url", "http://127.0.0.1:1", "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "planned"
    assert payload["requests"] == 100


def test_backup_bundle_uses_compose_client_when_host_pg_dump_is_missing():
    script = Path(__file__).resolve().parents[2] / "scripts" / "backup_bundle.py"
    spec = spec_from_file_location("backup_bundle", script)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    args = SimpleNamespace(
        host="127.0.0.1",
        port="55432",
        user="insight",
        database="insight",
        compose="compose.v2.yml",
        env_file=".env.local",
        postgres_service="postgres",
    )
    command, mode = module.build_dump_command(args, host_pg_dump=False)
    assert mode == "compose-pg_dump"
    assert command[:7] == [
        "docker",
        "compose",
        "--env-file",
        ".env.local",
        "-f",
        "compose.v2.yml",
        "exec",
    ]
    assert command[-5:] == ["--no-owner", "-U", "insight", "-d", "insight"]
