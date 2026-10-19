#!/usr/bin/env python3
"""Offline configuration checks; load secrets via uv --env-file, never print them."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def broker_check(value: str, network: str) -> bool:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname or not parsed.password:
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        if not parsed.path.strip("/").isdigit() or parsed.query or parsed.fragment:
            return False
        host = parsed.hostname.lower()
        if host in {"0.0.0.0", "::"}:
            return False
        loopback = host == "localhost"
        try:
            address = ipaddress.ip_address(host)
            loopback = loopback or address.is_loopback
            if address.is_unspecified or address.is_multicast:
                return False
        except ValueError:
            pass  # Private container DNS names are valid; reachability is a separate check.
        return not loopback or network == "host"
    except ValueError:
        return False


def inspect_configuration(environment, *, network="container", allow_root=False):
    hosts = [value.strip() for value in environment.get("DJANGO_ALLOWED_HOSTS", "").split(",") if value.strip()]
    storage = Path(environment.get("INSIGHT_STORAGE_ROOT", "backend/storage"))
    parent = storage if storage.exists() else storage.parent
    checks = {
        "debug_disabled": environment.get("DJANGO_DEBUG", "0") == "0",
        "secret_key_present": len(environment.get("DJANGO_SECRET_KEY", "")) >= 32,
        "database_password_present": bool(environment.get("INSIGHT_DB_PASSWORD", "")),
        "broker_url_and_network_valid": broker_check(environment.get("CELERY_BROKER_URL", ""), network),
        "allowed_hosts_explicit": bool(hosts) and "*" not in hosts and all("/" not in host and "://" not in host for host in hosts),
        "tls_termination_declared": environment.get("INSIGHT_TLS_TERMINATED", "0") == "1",
        "storage_absolute_and_writable_parent": storage.is_absolute() and parent.is_dir() and os.access(parent, os.W_OK),
        "non_root": allow_root or os.geteuid() != 0,
    }
    return {"schema_version": "production-preflight-v2", "status": "passed" if all(checks.values()) else "failed",
            "checks": checks, "broker_network": network,
            "scope": "offline_configuration_only; actual TLS, credentials, network, filesystem ownership and business security require deployment evidence"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--allow-root", action="store_true")
    parser.add_argument("--broker-network", choices=["container", "host"], default="container",
                        help="host permits an authenticated loopback broker for a declared host deployment")
    args = parser.parse_args()
    result = inspect_configuration(os.environ, network=args.broker_network, allow_root=args.allow_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
