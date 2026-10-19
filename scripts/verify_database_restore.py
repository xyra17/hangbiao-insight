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
parser.add_argument("--host", default=os.getenv("PGHOST", "127.0.0.1"))
parser.add_argument("--port", default=os.getenv("PGPORT", "55432"))
parser.add_argument("--output", default=".local/restore-drills")
parser.add_argument("--storage-root", default=os.getenv("INSIGHT_STORAGE_ROOT", "backend/storage"))
parser.add_argument("--credentials-file", type=Path)
parser.add_argument("--require-nonempty", action="store_true")
parser.add_argument("--skip-api-probe", action="store_true")
args = parser.parse_args()
os.umask(0o077)
repo_root = Path(__file__).resolve().parents[1]
storage_source = Path(args.storage_root).resolve()
if args.require_nonempty:
    if not args.database.startswith(("insight_acceptance", "insight_s7_profile")):
        parser.error("--require-nonempty is restricted to an explicitly isolated acceptance/profile database.")
    if ".local" not in storage_source.parts:
        parser.error("--require-nonempty requires a private .local storage root.")
    if not args.credentials_file or not args.credentials_file.is_file():
        parser.error("--require-nonempty needs a private local credentials file for restored API checks.")
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
    result = subprocess.run(base + command, check=False, stderr=subprocess.PIPE, **kwargs)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Restore drill command failed ({result.returncode}): {detail}")
    return result


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
        "memberships": "insight_membership",
        "shop_grants": "insight_shopgrant",
    }
    entries = [
        f"('{key}', (SELECT count(*)::bigint FROM {table}))"
        for key, table in objects.items()
    ]
    entries.extend(
        [
            "('semantic_citation_refs', (SELECT COALESCE(SUM(jsonb_array_length(CASE WHEN jsonb_typeof(payload->'citations') = 'array' THEN payload->'citations' ELSE '[]'::jsonb END)), 0)::bigint FROM insight_standardreport))",
            "('investigation_step_evidence_refs', (SELECT COALESCE(SUM(jsonb_array_length(evidence_refs)), 0)::bigint FROM insight_investigationstep))",
        ]
    )
    sql = "SELECT json_object_agg(key, value) FROM (VALUES " + ", ".join(entries) + ") AS counts(key, value);"
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


def integrity(database):
    sql = """
    SELECT json_build_object(
      'event_evidence_missing_event', (SELECT count(*) FROM insight_eventevidence e LEFT JOIN insight_businessevent b ON b.id=e.event_id WHERE b.id IS NULL),
      'event_evidence_missing_snapshot', (SELECT count(*) FROM insight_eventevidence e LEFT JOIN insight_metricsnapshot s ON s.id=e.snapshot_id WHERE s.id IS NULL),
      'reports_missing_semantic_run', (SELECT count(*) FROM insight_standardreport r LEFT JOIN insight_semanticrun s ON s.id=r.semantic_run_id WHERE r.semantic_run_id IS NOT NULL AND s.id IS NULL),
      'semantic_claim_missing_run', (SELECT count(*) FROM insight_semanticclaim c LEFT JOIN insight_semanticrun s ON s.id=c.run_id WHERE s.id IS NULL),
      'semantic_claim_missing_review', (SELECT count(*) FROM insight_semanticclaim c LEFT JOIN insight_review r ON r.id=c.review_id WHERE r.id IS NULL),
      'investigation_steps_missing_run', (SELECT count(*) FROM insight_investigationstep s LEFT JOIN insight_investigationrun r ON r.id=s.run_id WHERE r.id IS NULL),
      'investigation_tool_calls_missing_run', (SELECT count(*) FROM insight_investigationtoolcall c LEFT JOIN insight_investigationrun r ON r.id=c.run_id WHERE r.id IS NULL),
      'action_executions_missing_task', (SELECT count(*) FROM insight_actionexecution e LEFT JOIN insight_actiontask t ON t.id=e.task_id WHERE t.id IS NULL)
    );
    """
    result = execute(
        ["psql", "-U", args.user, "-d", database, "-At", "-c", sql],
        stdout=subprocess.PIPE,
    )
    return json.loads(result.stdout)


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
storage_root = storage_source
storage_archive = output / "storage.tar"
storage_files = [path for path in storage_root.rglob("*") if path.is_file()] if storage_root.exists() else []
source_storage_hashes = file_hashes(storage_root) if storage_root.exists() else {}
original_counts = counts(args.database)
original_integrity = integrity(args.database)
if args.require_nonempty:
    required_objects = ("reports", "investigations", "action_tasks", "semantic_runs", "semantic_citation_refs", "investigation_step_evidence_refs")
    missing = {name: original_counts.get(name, 0) for name in required_objects if not original_counts.get(name, 0)}
    if missing:
        raise RuntimeError(f"Nonempty backup drill fixtures are missing required business objects: {missing}")
    if any(original_integrity.values()):
        raise RuntimeError(f"Source database contains broken business references: {original_integrity}")
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
    restored_integrity = integrity(target)
    restored_storage = output / "restored-storage"
    if storage_files:
        restored_storage.mkdir()
        with tarfile.open(storage_archive) as archive:
            safe_extract(archive, restored_storage)
    restored_files = [path for path in restored_storage.rglob("*") if path.is_file()] if restored_storage.exists() else []
    restored_storage_hashes = file_hashes(restored_storage) if restored_storage.exists() else {}
    with dump.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    api_probe = None
    if not args.skip_api_probe:
        if not args.credentials_file:
            raise RuntimeError("API verification requires --credentials-file; pass --skip-api-probe only for a dump-only drill.")
        backend_root = repo_root / "backend"
        env_file = Path(os.path.relpath(Path(args.env_file).resolve(), backend_root))
        api_env = {
            **os.environ,
            "PGDATABASE": target,
            "PGHOST": args.host,
            "PGPORT": args.port,
            "PGUSER": args.user,
            "INSIGHT_STORAGE_ROOT": str(restored_storage),
        }
        probe = subprocess.run(
            [
                "uv", "run", "--directory", str(backend_root), "--env-file", str(env_file),
                "python", str(repo_root / "scripts" / "probe_restored_api.py"),
                "--credentials-file", str(args.credentials_file.resolve()),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=api_env,
            cwd=repo_root,
        )
        api_probe = (
            json.loads(probe.stdout)
            if probe.returncode == 0
            else {
                "status": "failed",
                "error": probe.stderr.strip()[-4000:],
                "exit_code": probe.returncode,
            }
        )
    record = {
        "status": (
            "passed"
            if api_probe and api_probe.get("status") == "passed"
            else "restored_api_unverified"
            if args.skip_api_probe
            else "verification_failed"
        ),
        "database": {"source_counts": original_counts, "restored_counts": restored},
        "restored_counts": restored,
        "reference_integrity": {"source": original_integrity, "restored": restored_integrity},
        "restored_api_probe": api_probe,
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
    if (
        restored != original_counts
        or source_storage_hashes != restored_storage_hashes
        or restored_integrity != original_integrity
        or any(restored_integrity.values())
    ):
        record["status"] = "verification_failed"
    (output / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if record["status"] != "passed":
        raise RuntimeError("联合恢复验证未通过；请查看 result.json 中的 API 或一致性结果。")
finally:
    if created:
        # Only the unique database created by this process can be removed.
        execute(["dropdb", "-U", args.user, target], stdout=subprocess.PIPE)
