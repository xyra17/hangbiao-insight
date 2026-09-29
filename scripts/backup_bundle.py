#!/usr/bin/env python3
"""Create a joint PostgreSQL + storage manifest for an isolated restore drill.

The script never writes credentials into the bundle.  It records object hashes,
database identity and timing so RPO/RTO can be measured by the restore script.
Use ``--dry-run`` in CI to validate the plan without touching a live database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_dump_command(args, *, host_pg_dump: bool | None = None) -> tuple[list[str], str]:
    """Choose a local pg_dump or the Compose PostgreSQL client.

    The explicit override keeps this decision deterministic in tests while the
    default checks the actual host.  The Compose path avoids requiring a
    PostgreSQL client installation on a developer or single-server host.
    """
    if host_pg_dump is None:
        host_pg_dump = shutil.which("pg_dump") is not None
    if host_pg_dump:
        return [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "-h",
            args.host,
            "-p",
            args.port,
            "-U",
            args.user,
            "-d",
            args.database,
        ], "host-pg_dump"
    return [
        "docker",
        "compose",
        "--env-file",
        args.env_file,
        "-f",
        args.compose,
        "exec",
        "-T",
        args.postgres_service,
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "-U",
        args.user,
        "-d",
        args.database,
    ], "compose-pg_dump"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".local/backups")
    parser.add_argument("--storage-root", default=os.getenv("INSIGHT_STORAGE_ROOT", "backend/storage"))
    parser.add_argument("--database", default=os.getenv("PGDATABASE", "insight"))
    parser.add_argument("--user", default=os.getenv("PGUSER", "insight"))
    parser.add_argument("--host", default=os.getenv("PGHOST", "127.0.0.1"))
    parser.add_argument("--port", default=os.getenv("PGPORT", "55432"))
    parser.add_argument(
        "--compose",
        default="compose.v2.yml",
        help="Compose 文件；宿主机没有 pg_dump 时用于在 postgres 容器内导出",
    )
    parser.add_argument("--env-file", default=".env.local")
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle = output / f"insight-{stamp}"
    bundle.mkdir(mode=0o700)
    storage = Path(args.storage_root)
    files = [path for path in storage.rglob("*") if path.is_file()] if storage.exists() else []
    manifest = {
        "schema": "insight-joint-backup-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": {"name": args.database, "user": args.user, "host": args.host, "port": args.port},
        "storage_root": str(storage),
        "files": [{"relative": str(path.relative_to(storage)), "size": path.stat().st_size, "sha256": sha256(path)} for path in files],
        "rpo_target_minutes": int(os.getenv("INSIGHT_RPO_TARGET_MINUTES", "60")),
        "rto_target_minutes": int(os.getenv("INSIGHT_RTO_TARGET_MINUTES", "120")),
        "dry_run": args.dry_run,
    }
    if not args.dry_run:
        dump = bundle / "database.dump"
        dump_command, dump_mode = build_dump_command(args)
        with dump.open("wb") as stream:
            subprocess.run(dump_command, check=True, stdout=stream)
        if files:
            with tarfile.open(bundle / "storage.tar", "w") as archive:
                for path in files:
                    archive.add(path, arcname=str(path.relative_to(storage)))
        manifest["database_dump_sha256"] = sha256(dump)
        manifest["database_dump_mode"] = dump_mode
        manifest["storage_archive"] = "storage.tar" if files else None
    manifest["duration_seconds"] = round(time.monotonic() - started, 3)
    (bundle / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"bundle": str(bundle), **manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
