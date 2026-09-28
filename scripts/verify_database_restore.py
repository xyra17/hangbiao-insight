"""Non-destructive PostgreSQL dump/restore drill into a uniquely named database.

The dump contains sensitive data. Output directories are private. No production
restore or storage-file consistency is claimed by this database-only drill.
"""

import argparse
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--compose", default="compose.dev.yml")
parser.add_argument("--env-file", default=".env.local")
parser.add_argument("--database", default="insight")
parser.add_argument("--user", default="insight")
parser.add_argument("--output", default=".local/restore-drills")
args = parser.parse_args()
os.umask(0o077)
output = Path(args.output) / uuid.uuid4().hex
output.mkdir(parents=True, exist_ok=False)
base = [
    "docker",
    "compose",
    "--env-file",
    args.env_file,
    "-f",
    args.compose,
    "exec",
    "-T",
    "postgres",
]
target = "insight_restore_check_" + uuid.uuid4().hex[:16]


def execute(command, **kwargs):
    return subprocess.run(base + command, check=True, stderr=subprocess.PIPE, **kwargs)


def counts(database):
    sql = """SELECT json_build_object(
      'migrations', (SELECT count(*) FROM django_migrations),
      'sources', (SELECT count(*) FROM insight_source),
      'raw_files', (SELECT count(*) FROM insight_rawfile),
      'reviews', (SELECT count(*) FROM insight_review),
      'snapshots', (SELECT count(*) FROM insight_metricsnapshot),
      'reports', (SELECT count(*) FROM insight_standardreport),
      'investigations', (SELECT count(*) FROM insight_investigationrun));"""
    result = execute(
        ["psql", "-U", args.user, "-d", database, "-At", "-c", sql],
        stdout=subprocess.PIPE,
    )
    return json.loads(result.stdout)


started = time.monotonic()
dump = output / "database.dump"
with dump.open("xb") as stream:
    execute(
        ["pg_dump", "-U", args.user, "-d", args.database, "--format=custom"],
        stdout=stream,
    )
created = False
try:
    execute(["createdb", "-U", args.user, target], stdout=subprocess.PIPE)
    created = True
    with dump.open("rb") as stream:
        execute(
            [
                "pg_restore",
                "-U",
                args.user,
                "-d",
                target,
                "--exit-on-error",
                "--no-owner",
            ],
            stdin=stream,
            stdout=subprocess.PIPE,
        )
    restored = counts(target)
    with dump.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    record = {
        "status": "restored",
        "restored_counts": restored,
        "duration_seconds": round(time.monotonic() - started, 2),
        "dump_sha256": checksum,
        "limitations": "Database-only drill; no file-store recovery, point-in-time comparison, RPO/RTO or production acceptance.",
    }
    (output / "result.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record, indent=2))
finally:
    if created:
        # Only the unique database created by this process can be removed.
        execute(["dropdb", "-U", args.user, target], stdout=subprocess.PIPE)
