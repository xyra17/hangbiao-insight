"""Non-destructive joint PostgreSQL and file-store restore drill.

The dump contains sensitive data. Output directories are private. The target
database is always uniquely named and dropped after verification.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--compose", default="compose.v2.yml")
parser.add_argument("--env-file", default=".env.local")
parser.add_argument("--database", default="insight")
parser.add_argument("--user", default="insight")
parser.add_argument("--output", default=".local/restore-drills")
parser.add_argument("--storage-root", default=os.getenv("INSIGHT_STORAGE_ROOT", "backend/storage"))
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
    # Keep this list explicit: a restore drill must fail when a newly added
    # business object is forgotten, rather than silently validating only the
    # original small subset of tables.
    objects = {
        "migrations": "django_migrations",
        "workspaces": "insight_workspace",
        "shops": "insight_shop",
        "products": "insight_product",
        "audits": "insight_auditentry",
        "jobs": "insight_job",
        "outbox": "insight_outbox",
        "sources": "insight_source",
        "source_contracts": "insight_sourcecontract",
        "raw_files": "insight_rawfile",
        "import_batches": "insight_importbatch",
        "staged_rows": "insight_stagedrow",
        "retractions": "insight_factretraction",
        "sales_traffic": "insight_salestrafficdaily",
        "orders": "insight_orderline",
        "refunds": "insight_refundtransaction",
        "returns": "insight_returnlifecycle",
        "reviews": "insight_review",
        "support_cases": "insight_supportcase",
        "inventory": "insight_inventorysnapshot",
        "ratings": "insight_ratingsnapshot",
        "operation_events": "insight_operationevent",
        "snapshots": "insight_metricsnapshot",
        "parent_snapshots": "insight_parentsnapshot",
        "observation_leads": "insight_observationlead",
        "semantic_runs": "insight_semanticrun",
        "semantic_claims": "insight_semanticclaim",
        "semantic_review_results": "insight_semanticreviewresult",
        "reports": "insight_standardreport",
        "action_tasks": "insight_actiontask",
        "action_executions": "insight_actionexecution",
        "change_proposals": "insight_changeproposal",
        "investigations": "insight_investigationrun",
        "investigation_steps": "insight_investigationstep",
        "investigation_decisions": "insight_investigationdecision",
        "investigation_tool_calls": "insight_investigationtoolcall",
        "event_groups": "insight_eventgroup",
        "business_events": "insight_businessevent",
        "event_evidence": "insight_eventevidence",
        "notifications": "insight_notification",
        "daily_schedules": "insight_dailyscanschedule",
        "rule_configurations": "insight_ruleconfiguration",
        "command_receipts": "insight_investigationcommandreceipt",
        "budget_ledger": "insight_investigationbudgetledger",
        "retention_tombstones": "insight_retentiontombstone",
        "grouping_receipts": "insight_eventgroupingreceipt",
        "derived_refreshes": "insight_derivedrefresh",
    }
    expression = ",\n      ".join(
        f"'{key}', (SELECT count(*) FROM {table})" for key, table in objects.items()
    )
    sql = f"SELECT json_build_object(\n      {expression});"
    result = execute(
        ["psql", "-U", args.user, "-d", database, "-At", "-c", sql],
        stdout=subprocess.PIPE,
    )
    return json.loads(result.stdout)


def file_hashes(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as handle:
                result[str(path.relative_to(root))] = hashlib.file_digest(handle, "sha256").hexdigest()
    return result


def safe_extract(archive, destination):
    destination = destination.resolve()
    for member in archive.getmembers():
        relative = PurePosixPath(member.name)
        if relative.is_absolute() or ".." in relative.parts or member.issym() or member.islnk():
            raise RuntimeError(f"storage archive contains an unsafe path: {member.name}")
        target = (destination / Path(*relative.parts)).resolve()
        if target != destination and destination not in target.parents:
            raise RuntimeError(f"storage archive contains an unsafe path: {member.name}")
    archive.extractall(destination)


started = time.monotonic()
dump = output / "database.dump"
storage_root = Path(args.storage_root)
storage_archive = output / "storage.tar"
storage_files = [path for path in storage_root.rglob("*") if path.is_file()] if storage_root.exists() else []
source_storage_hashes = file_hashes(storage_root) if storage_root.exists() else {}
original_counts = counts(args.database)
if storage_files:
    with tarfile.open(storage_archive, "w") as archive:
        for path in storage_files:
            archive.add(path, arcname=str(path.relative_to(storage_root)))
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
    restored_storage = output / "restored-storage"
    if storage_files:
        restored_storage.mkdir()
        with tarfile.open(storage_archive) as archive:
            safe_extract(archive, restored_storage)
    restored_files = [path for path in restored_storage.rglob("*") if path.is_file()] if restored_storage.exists() else []
    restored_storage_hashes = file_hashes(restored_storage) if restored_storage.exists() else {}
    with dump.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    record = {
        "status": "restored",
        "database": {"source_counts": original_counts, "restored_counts": restored},
        "restored_counts": restored,
        "duration_seconds": round(time.monotonic() - started, 2),
        "dump_sha256": checksum,
        "storage": {
            "source_files": len(storage_files),
            "restored_files": len(restored_files),
            "hashes_match": source_storage_hashes == restored_storage_hashes,
            "archive": str(storage_archive) if storage_files else None,
        },
        "rpo_minutes": None,
        "rto_seconds": round(time.monotonic() - started, 2),
        "limitations": "隔离恢复演练；生产 RPO/RTO、TLS、容量和业务签收仍需目标环境证据。",
    }
    (output / "result.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record, indent=2))
    if restored != original_counts or source_storage_hashes != restored_storage_hashes:
        record["status"] = "verification_failed"
        (output / "result.json").write_text(json.dumps(record, indent=2))
        raise RuntimeError("联合恢复对象或文件校验值不一致")
finally:
    if created:
        # Only the unique database created by this process can be removed.
        execute(["dropdb", "-U", args.user, target], stdout=subprocess.PIPE)
