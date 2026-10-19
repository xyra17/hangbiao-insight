import importlib.util
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "dataset" / "run_capacity_acceptance.py"
SPEC = importlib.util.spec_from_file_location("capacity_acceptance_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_login_rotation_updates_csrf_header_for_scan_posts():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/session/":
            return httpx.Response(
                200,
                json={"authenticated": False, "csrf_token": "csrf-before-login"},
                headers={"set-cookie": "csrftoken=csrf-before-login; Path=/"},
            )
        if request.url.path == "/api/v1/login/":
            assert request.headers["X-CSRFToken"] == "csrf-before-login"
            return httpx.Response(
                200,
                json={"username": "capacity-admin", "csrf_token": "csrf-after-login"},
                headers=[
                    ("set-cookie", "csrftoken=csrf-after-login; Path=/"),
                    ("set-cookie", "sessionid=local-session; Path=/"),
                ],
            )
        raise AssertionError(f"Unexpected request path: {request.url.path}")

    client = runner.new_authenticated_client(
        "http://127.0.0.1:8280",
        "capacity-admin",
        "test-password",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.headers["X-CSRFToken"] == "csrf-after-login"
        assert client.cookies.get("csrftoken") == "csrf-after-login"
        assert len(seen) == 2
    finally:
        client.close()


def test_http_failure_record_keeps_status_and_path_without_response_body():
    request = httpx.Request("POST", "http://127.0.0.1:8280/api/v1/snapshots/scan/")
    response = httpx.Response(500, request=request, text="sensitive response payload")
    error = httpx.HTTPStatusError("request failed", request=request, response=response)

    failure = runner.failure_record("test-shop", "CAPACITY01", error)

    assert failure == {
        "shop": "test-shop",
        "asin": "CAPACITY01",
        "error_type": "HTTPStatusError",
        "http_status": 500,
        "request_path": "/api/v1/snapshots/scan/",
    }
    assert "sensitive response payload" not in str(failure)


def test_capacity_runner_loads_distinct_accounts_without_exposing_passwords(tmp_path):
    credentials = tmp_path / "accounts.json"
    credentials.write_text(
        json.dumps(
            {"accounts": [{"username": f"operator-{index}", "password": f"secret-{index}"} for index in range(10)]}
        ),
        encoding="utf-8",
    )

    accounts = runner.load_accounts(credentials, None, None)

    assert len(accounts) == 10
    assert len({account["username"] for account in accounts}) == 10
    assert all(account["password"] for account in accounts)


def test_scan_runner_reports_queue_worker_and_correctness_separately():
    job_id = "test-job"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/snapshots/scan/":
            return httpx.Response(202, json={"id": job_id}, request=request)
        if request.method == "GET" and request.url.path == f"/api/v1/jobs/{job_id}/":
            return httpx.Response(
                200,
                json={
                    "id": job_id,
                    "status": "completed",
                    "created_at": "2026-10-04T00:00:00Z",
                    "started_at": "2026-10-04T00:00:01Z",
                    "heartbeat_at": "2026-10-04T00:00:01Z",
                    "finished_at": "2026-10-04T00:00:05Z",
                    "result": {"snapshot_id": "snapshot-1"},
                },
                request=request,
            )
        if request.method == "GET" and request.url.path == "/api/v1/snapshots/":
            return httpx.Response(
                200,
                json={"results": [{"id": "snapshot-1", "payload": {"daily": {"status": "ready"}}}]},
                request=request,
            )
        raise AssertionError(f"Unexpected request path: {request.url.path}")

    with httpx.Client(base_url="http://127.0.0.1:8280", transport=httpx.MockTransport(handler)) as client:
        result = runner.scan_with_client(client, "test-shop", "CAPACITY01", 1)

    assert result["request_success"] is True
    assert result["job_status"] == "completed"
    assert result["correctness_status"] == "passed"
    assert result["job_queue_wait_ms"] == 1000.0
    assert result["job_worker_runtime_ms"] == 4000.0
    assert result["job_timing_status"] == "observed"
    assert result["enqueued_to_terminal_ms"] >= 0


def test_scan_runner_does_not_infer_start_from_the_final_heartbeat():
    job_id = "fast-job"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/snapshots/scan/":
            return httpx.Response(202, json={"id": job_id}, request=request)
        if request.method == "GET" and request.url.path == f"/api/v1/jobs/{job_id}/":
            return httpx.Response(
                200,
                json={
                    "id": job_id,
                    "status": "completed",
                    "created_at": "2026-10-04T00:00:00Z",
                    "heartbeat_at": "2026-10-04T00:00:02Z",
                    "finished_at": "2026-10-04T00:00:02.050000Z",
                    "result": {"snapshot_id": "snapshot-1"},
                },
                request=request,
            )
        if request.method == "GET" and request.url.path == "/api/v1/snapshots/":
            return httpx.Response(
                200,
                json={"results": [{"id": "snapshot-1", "payload": {"daily": {"status": "ready"}}}]},
                request=request,
            )
        raise AssertionError(f"Unexpected request path: {request.url.path}")

    with httpx.Client(base_url="http://127.0.0.1:8280", transport=httpx.MockTransport(handler)) as client:
        result = runner.scan_with_client(client, "test-shop", "CAPACITY01", 1)

    assert result["job_status"] == "completed"
    assert result["correctness_status"] == "passed"
    assert result["job_queue_wait_ms"] is None
    assert result["job_worker_runtime_ms"] is None
    assert result["job_timing_status"] == "start_not_recorded"


def test_docker_resource_sampler_filters_to_the_requested_compose_project(monkeypatch):
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[1] == "ps":
            return subprocess.CompletedProcess(command, 0, stdout="capacity-api-id\n", stderr="")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"Name": "s7-api-1", "CPUPerc": "12.5%", "MemUsage": "128MiB / 512MiB"}) + "\n",
            stderr="",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    samples, error = runner.sample_docker_resources("s7-capacity-only")

    assert error is None
    assert samples["api"]["container_count"] == 1
    assert samples["api"]["cpu_pct"] == 12.5
    assert "label=com.docker.compose.project=s7-capacity-only" in commands[0]
    assert commands[1][-1] == "capacity-api-id"


def test_business_api_sampler_distinguishes_populated_multi_user_samples(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "manifest.json").write_text(
        json.dumps({"coverage_window": {"start": "2026-04-04", "end": "2026-09-30"}}),
        encoding="utf-8",
    )

    class FakeClient:
        def get(self, path, params=None):
            request = httpx.Request("GET", f"http://127.0.0.1{path}?{urlencode(params or {})}")
            if path == "/api/v1/products/":
                payload = {"results": [{"id": "product-1", "child_asin": "CAPACITY01"}], "count": 1}
            else:
                payload = {"results": [{"id": "row-1"}], "count": 1}
            return httpx.Response(200, json=payload, request=request)

        def post(self, path, json=None):
            request = httpx.Request("POST", f"http://127.0.0.1{path}")
            return httpx.Response(200, json={"items": [{"id": "fact-1"}]}, request=request)

    @contextmanager
    def fake_authenticated_client(_base_url, _username, _password):
        yield FakeClient()

    monkeypatch.setattr(runner, "authenticated_client", fake_authenticated_client)
    accounts = [{"username": f"operator-{index}", "password": "private"} for index in range(10)]

    result = runner.sample_business_apis("http://127.0.0.1:8280", accounts, ["shop-a", "shop-b"], pack)

    assert result["status"] == "passed"
    assert result["distinct_account_count"] == 10
    assert result["population_check"] == "passed"
    assert result["endpoint_stats"]["reports"]["nonempty_sample_count"] == 10
    assert result["endpoint_stats"]["evidence_search"]["request_success_count"] == 10


def test_business_api_sampler_records_expected_source_registry_denials_for_scoped_operators(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "manifest.json").write_text(
        json.dumps({"coverage_window": {"start": "2026-10-05", "end": "2026-10-05"}}),
        encoding="utf-8",
    )

    class FakeClient:
        def get(self, path, params=None):
            request = httpx.Request("GET", f"http://127.0.0.1{path}")
            if path == "/api/v1/sources/":
                return httpx.Response(403, json={"detail": "forbidden"}, request=request)
            rows = [{"id": "product-1", "child_asin": "CAPACITY01"}] if path == "/api/v1/products/" else [{"id": "row-1"}]
            return httpx.Response(200, json={"results": rows, "count": len(rows)}, request=request)

        def post(self, path, json=None):
            request = httpx.Request("POST", f"http://127.0.0.1{path}")
            return httpx.Response(200, json={"items": [{"id": "fact-1"}]}, request=request)

    @contextmanager
    def fake_authenticated_client(_base_url, _username, _password):
        yield FakeClient()

    monkeypatch.setattr(runner, "authenticated_client", fake_authenticated_client)
    accounts = [
        {
            "username": f"operator-{index}",
            "password": "private",
            "account_class": "synthetic_isolated_capacity_load",
            "shop_ids": ["shop-a", "shop-b"],
        }
        for index in range(10)
    ]

    result = runner.sample_business_apis("http://127.0.0.1:8280", accounts, ["shop-a", "shop-b"], pack)

    sources = result["endpoint_stats"]["sources"]
    assert result["status"] == "passed"
    assert result["request_success"] == "expected_authorization_denials_only"
    assert result["expected_permission_denial_count"] == 10
    assert sources["request_success_count"] == 0
    assert sources["expected_permission_denial_count"] == 10
    assert sources["unexpected_failure_count"] == 0
    assert sources["failures"] == []


def test_business_api_sampler_keeps_failures_attached_to_the_account_that_failed(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "manifest.json").write_text(
        json.dumps({"coverage_window": {"start": "2026-04-04", "end": "2026-09-30"}}),
        encoding="utf-8",
    )

    class FakeClient:
        def get(self, path, params=None):
            request = httpx.Request("GET", f"http://127.0.0.1{path}")
            return httpx.Response(200, json={"results": [], "count": 0}, request=request)

    @contextmanager
    def fake_authenticated_client(_base_url, username, _password):
        if username == "operator-fails":
            raise httpx.ConnectError("local test failure")
        yield FakeClient()

    monkeypatch.setattr(runner, "authenticated_client", fake_authenticated_client)
    accounts = [
        {"username": "operator-fails", "password": "private"},
        {"username": "operator-works", "password": "private"},
    ]

    result = runner.sample_business_apis("http://127.0.0.1:8280", accounts, ["shop-a"], pack)

    auth_stats = result["endpoint_stats"]["authentication"]
    assert auth_stats["account_indices"] == [1]
    assert auth_stats["failures"] == [{"error_type": "ConnectError"}]
    assert result["status"] == "failed"


def test_business_api_sampler_never_uses_a_shop_outside_account_scope(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "manifest.json").write_text(
        json.dumps({"coverage_window": {"start": "2026-04-04", "end": "2026-09-30"}}),
        encoding="utf-8",
    )
    requested_shops = []

    class FakeClient:
        def get(self, path, params=None):
            requested_shops.append(params["shop"])
            request = httpx.Request("GET", f"http://127.0.0.1{path}")
            return httpx.Response(200, json={"results": [], "count": 0}, request=request)

        def post(self, path, json=None):
            raise AssertionError("The out-of-scope shop must not be searched.")

    @contextmanager
    def fake_authenticated_client(_base_url, _username, _password):
        yield FakeClient()

    monkeypatch.setattr(runner, "authenticated_client", fake_authenticated_client)
    accounts = [{"username": "limited", "password": "private", "shop_ids": ["shop-b"]}]

    result = runner.sample_business_apis("http://127.0.0.1:8280", accounts, ["shop-a", "shop-b"], pack)

    assert requested_shops
    assert set(requested_shops) == {"shop-b"}
    assert result["endpoint_stats"]["events"]["shop_ids"] == ["shop-b"]


def test_business_api_sampler_marks_accounts_without_any_sample_scope_as_failed(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "manifest.json").write_text(
        json.dumps({"coverage_window": {"start": "2026-04-04", "end": "2026-09-30"}}),
        encoding="utf-8",
    )
    accounts = [{"username": "limited", "password": "private", "shop_ids": ["unrelated-shop"]}]

    result = runner.sample_business_apis("http://127.0.0.1:8280", accounts, ["shop-a"], pack)

    assert result["status"] == "failed"
    assert result["endpoint_stats"]["authorization_scope"]["failures"] == [
        {"error_type": "no_authorized_sample_shop"}
    ]
