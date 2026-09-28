#!/usr/bin/env python3
"""Fail-fast checks for a single-server production deployment."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--allow-root", action="store_true")
    args = parser.parse_args()
    checks = {}
    checks["debug_disabled"] = os.getenv("DJANGO_DEBUG", "0") != "1"
    checks["secret_key_present"] = len(os.getenv("DJANGO_SECRET_KEY", "")) >= 32
    checks["database_password_present"] = bool(os.getenv("INSIGHT_DB_PASSWORD"))
    checks["broker_is_remote"] = not os.getenv("CELERY_BROKER_URL", "").startswith(("redis://127.0.0.1", "redis://localhost"))
    checks["allowed_hosts_configured"] = bool(os.getenv("DJANGO_ALLOWED_HOSTS"))
    checks["tls_termination_declared"] = os.getenv("INSIGHT_TLS_TERMINATED", "0") == "1"
    storage = Path(os.getenv("INSIGHT_STORAGE_ROOT", "backend/storage"))
    checks["storage_root_configured"] = storage.is_absolute() and (storage.exists() or storage.parent.exists())
    checks["non_root"] = args.allow_root or os.geteuid() != 0
    result = {"status": "passed" if all(checks.values()) else "failed", "checks": checks}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
