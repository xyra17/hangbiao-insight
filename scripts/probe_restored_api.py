"""Exercise read-only business APIs against a restored isolated database."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import django

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def load_account(path: Path) -> tuple[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    accounts = payload if isinstance(payload, list) else payload.get("accounts", [payload])
    account = next((item for item in accounts if item.get("username") == "accept_admin"), None)
    if account is None:
        raise RuntimeError("The private credentials file must contain the acceptance administrator.")
    return account["username"], account["password"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--gone-import-batch")
    args = parser.parse_args()
    django.setup()
    from rest_framework.test import APIClient

    username, password = load_account(args.credentials_file)
    client = APIClient(enforce_csrf_checks=True)
    token = client.get("/api/v1/session/").data["csrf_token"]
    login = client.post(
        "/api/v1/login/",
        {"username": username, "password": password},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )
    if login.status_code != 200:
        raise RuntimeError(f"Restored API login failed with HTTP {login.status_code}.")

    checks = {}

    def collection(name: str, path: str) -> list[dict]:
        response = client.get(path)
        if response.status_code != 200:
            raise RuntimeError(f"Restored {name} list API failed with HTTP {response.status_code}.")
        payload = response.data
        results = payload.get("results", [])
        checks[name + "_list"] = {
            "status": "passed",
            "http_status": response.status_code,
            "reported_count": payload.get("count", len(results)),
            "visible_rows": len(results),
        }
        return results

    reports = collection("reports", "/api/v1/reports/")
    investigations = collection("investigations", "/api/v1/investigations/")
    actions = collection("action_tasks", "/api/v1/action-tasks/")
    events = collection("events", "/api/v1/events/")
    snapshots = collection("snapshots", "/api/v1/snapshots/")

    detail_checks = (
        ("report_detail", reports, "/api/v1/reports/"),
        ("investigation_detail", investigations, "/api/v1/investigations/"),
        ("action_detail", actions, "/api/v1/action-tasks/"),
        ("event_detail", events, "/api/v1/events/"),
        ("snapshot_detail", snapshots, "/api/v1/snapshots/"),
    )
    for name, rows, prefix in detail_checks:
        if not rows:
            raise RuntimeError(f"Restored {name} API has no visible acceptance fixture to inspect.")
        response = client.get(f"{prefix}{rows[0]['id']}/")
        if response.status_code != 200:
            raise RuntimeError(f"Restored {name} API failed with HTTP {response.status_code}.")
        checks[name] = {"status": "passed", "http_status": response.status_code}

    if not events:
        raise RuntimeError("Restored event evidence fixture is missing.")
    evidence_response = next(
        (
            response
            for event in events
            if (response := client.get(f"/api/v1/events/{event['id']}/evidence/")).status_code == 200
            and response.data.get("results")
        ),
        None,
    )
    if evidence_response is None:
        raise RuntimeError("Restored event evidence API returned no evidence rows.")
    checks["event_evidence"] = {
        "status": "passed",
        "http_status": evidence_response.status_code,
        "reported_count": evidence_response.data.get("count", len(evidence_response.data["results"])),
    }

    if args.gone_import_batch:
        gone = client.get(f"/api/v1/imports/{args.gone_import_batch}/original-file/")
        if gone.status_code != 410:
            raise RuntimeError(f"Expired original-file API returned HTTP {gone.status_code}, expected 410.")
        checks["expired_original_file"] = {"status": "passed", "http_status": gone.status_code}
    logout = client.post("/api/v1/logout/", HTTP_X_CSRFTOKEN=login.data["csrf_token"])
    if logout.status_code != 204:
        raise RuntimeError(f"Restored API logout failed with HTTP {logout.status_code}.")
    print(json.dumps({"status": "passed", "checks": checks}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
