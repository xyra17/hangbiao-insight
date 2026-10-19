"""Run a retention cleanup drill on a disposable copy of the local acceptance database."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def run(command: list[str], *, cwd: Path = ROOT, env: dict | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: {command[0]} {command[1]}\n"
            f"{detail[-3000:]}"
        )
    return result


def django_code(code: str, database: str, storage_root: Path, retention_days: int = 180) -> str:
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": os.getenv("DJANGO_SETTINGS_MODULE", "config.settings"),
        "PGDATABASE": database,
        "PGHOST": "127.0.0.1",
        "PGPORT": os.getenv("PGPORT", "55432"),
        "PGUSER": os.getenv("PGUSER", "insight"),
        "INSIGHT_STORAGE_ROOT": str(storage_root),
        "INSIGHT_RETENTION_DAYS": str(retention_days),
    }
    result = run(
        [
            "uv", "run", "--directory", str(BACKEND), "--env-file", "../.env.local",
            "python", "-c", "import django; django.setup();\n" + code,
        ],
        env=env,
    )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-database", default="insight_acceptance")
    parser.add_argument("--ownership-file", type=Path, help="required for named release databases; binds database and run directory")
    parser.add_argument("--compose", default="compose.dev.yml")
    parser.add_argument("--env-file", default=".env.local")
    parser.add_argument("--user", default=os.getenv("PGUSER", "insight"))
    parser.add_argument("--storage-root", default=".local/acceptance-storage")
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--output", default=".local/retention-drills")
    args = parser.parse_args()
    source_storage = Path(args.storage_root).resolve()
    credential_file = args.credentials_file.resolve()
    if args.source_database != "insight_acceptance":
        owner_path = args.ownership_file.resolve() if args.ownership_file else None
        if not owner_path or not owner_path.is_relative_to(ROOT / ".local/acceptance-artifacts") or not owner_path.is_file():
            parser.error("Named release databases require a local run ownership file.")
        owner = json.loads(owner_path.read_text())
        if owner.get("database") != args.source_database or not owner_path.is_relative_to(Path(owner.get("run_dir", "/invalid"))):
            parser.error("Database does not match the explicit run ownership record.")
    if ".local" not in source_storage.parts or ".local" not in credential_file.parts:
        parser.error("Storage and credentials must stay under the repository .local directory.")
    if not credential_file.is_file() or not source_storage.is_dir():
        parser.error("The private acceptance credentials and storage fixtures must already exist.")

    os.umask(0o077)
    output_root = Path(args.output).resolve()
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_id = uuid.uuid4().hex
    output = output_root / run_id
    output.mkdir(mode=0o700)
    target = "insight_retention_" + run_id[:12]
    clone_storage = output / "storage"
    shutil.copytree(source_storage, clone_storage)
    compose = [
        "docker", "compose", "--env-file", args.env_file, "-f", args.compose, "exec", "-T", "postgres"
    ]
    created = False
    result_record = {"status": "running", "source_database": args.source_database, "clone_database": target}
    try:
        run(compose + ["createdb", "-U", args.user, "-T", args.source_database, target])
        created = True
        setup_code = '''
import json
from datetime import timedelta
from django.utils import timezone
from insight.models import ActionTask, BusinessEvent, EventEvidence, ImportBatch, InvestigationRun, MetricSnapshot, RawFile, RetentionTombstone, SemanticRun, SourceContract, StandardReport
raw = RawFile.objects.filter(storage_key__gt="").order_by("created_at", "id").first()
if raw is None:
    raise RuntimeError("No stored synthetic acceptance file is available for the clone drill.")
batches = list(ImportBatch.objects.filter(raw_file=raw).select_related("contract__source__shop"))
if not batches:
    raise RuntimeError("The selected raw file has no import batch reference.")
contract_ids = {batch.contract_id for batch in batches}
SourceContract.objects.filter(pk__in=contract_ids).update(retention_days=180)
batch_ids = [str(batch.pk) for batch in batches]
ImportBatch.objects.filter(pk__in=batch_ids).update(created_at=timezone.now() - timedelta(days=181))
citations = sum(len(report.payload.get("citations", [])) for report in StandardReport.objects.all())
data = {
    "workspace_id": str(batches[0].contract.source.shop.workspace_id),
    "raw_file_id": str(raw.pk),
    "raw_file_sha256": raw.sha256,
    "batch_ids": batch_ids,
    "batch_id_for_410": batch_ids[0],
    "file_exists_before": True,
    "business_counts": {
        "reports": StandardReport.objects.count(),
        "report_citations": citations,
        "semantic_runs": SemanticRun.objects.count(),
        "investigations": InvestigationRun.objects.count(),
        "actions": ActionTask.objects.count(),
        "events": BusinessEvent.objects.count(),
        "event_evidence": EventEvidence.objects.count(),
        "snapshots": MetricSnapshot.objects.count(),
    },
    "staged_rows_before": sum(batch.rows.count() for batch in batches),
    "tombstones_before": RetentionTombstone.objects.count(),
}
print(json.dumps(data, ensure_ascii=False))
'''
        fixture = json.loads(django_code(setup_code, target, clone_storage))
        raw_file_path = clone_storage / fixture["raw_file_sha256"]
        if not raw_file_path.is_file():
            raise RuntimeError("The cloned storage fixture does not contain the selected raw payload.")
        fixture["file_exists_before"] = True

        dry_run = run(
            [
                "uv", "run", "--directory", str(BACKEND), "--env-file", "../.env.local",
                "python", "manage.py", "cleanup_retention", "--workspace", fixture["workspace_id"], "--dry-run",
            ],
            env={
                **os.environ,
                "PGDATABASE": target,
                "PGHOST": "127.0.0.1",
                "PGPORT": os.getenv("PGPORT", "55432"),
                "PGUSER": args.user,
                "INSIGHT_STORAGE_ROOT": str(clone_storage),
                "INSIGHT_RETENTION_DAYS": "180",
            },
        )
        dry_summary = json.loads(dry_run.stdout)
        if dry_summary["raw_files"] < 1:
            raise RuntimeError("Dry-run did not identify the intentionally expired clone payload.")

        actual = run(
            [
                "uv", "run", "--directory", str(BACKEND), "--env-file", "../.env.local",
                "python", "manage.py", "cleanup_retention", "--workspace", fixture["workspace_id"],
            ],
            env={
                **os.environ,
                "PGDATABASE": target,
                "PGHOST": "127.0.0.1",
                "PGPORT": os.getenv("PGPORT", "55432"),
                "PGUSER": args.user,
                "INSIGHT_STORAGE_ROOT": str(clone_storage),
                "INSIGHT_RETENTION_DAYS": "180",
            },
        )
        cleanup_summary = json.loads(actual.stdout)
        verify_code = '''
import json
from insight.models import ActionTask, BusinessEvent, EventEvidence, ImportBatch, InvestigationRun, MetricSnapshot, RawFile, RetentionTombstone, SemanticRun, StandardReport, StagedRow
raw = RawFile.objects.get(pk=RAW_ID)
batches = list(ImportBatch.objects.filter(pk__in=BATCH_IDS))
citations = sum(len(report.payload.get("citations", [])) for report in StandardReport.objects.all())
print(json.dumps({
  "raw_deleted_at": raw.deleted_at is not None,
  "raw_deletion_reason": raw.deletion_reason,
  "raw_storage_key_cleared": raw.storage_key == "",
  "raw_size_cleared": raw.size == 0,
  "tombstone_exists": RetentionTombstone.objects.filter(object_type="raw_file", object_id=RAW_ID, content_hash=raw.sha256).exists(),
  "batch_count_retained": len(batches),
  "staged_rows_retained": sum(StagedRow.objects.filter(batch=batch).count() for batch in batches),
  "business_counts": {
    "reports": StandardReport.objects.count(),
    "report_citations": citations,
    "semantic_runs": SemanticRun.objects.count(),
    "investigations": InvestigationRun.objects.count(),
    "actions": ActionTask.objects.count(),
    "events": BusinessEvent.objects.count(),
    "event_evidence": EventEvidence.objects.count(),
    "snapshots": MetricSnapshot.objects.count(),
  },
}, ensure_ascii=False))
'''.replace("RAW_ID", repr(fixture["raw_file_id"])).replace("BATCH_IDS", repr(fixture["batch_ids"]))
        verification = json.loads(django_code(verify_code, target, clone_storage))
        if raw_file_path.exists():
            raise RuntimeError("Expired raw payload still exists after real cleanup on the clone.")
        if not all(
            [
                verification["raw_deleted_at"],
                verification["raw_deletion_reason"] == "保留期清理完成",
                verification["raw_storage_key_cleared"],
                verification["raw_size_cleared"],
                verification["tombstone_exists"],
                verification["batch_count_retained"] == len(fixture["batch_ids"]),
                verification["staged_rows_retained"] == fixture["staged_rows_before"],
                verification["business_counts"] == fixture["business_counts"],
            ]
        ):
            raise RuntimeError(f"Retention clone verification mismatch: {verification}")

        probe_env = {
            **os.environ,
            "PGDATABASE": target,
            "PGHOST": "127.0.0.1",
            "PGPORT": os.getenv("PGPORT", "55432"),
            "PGUSER": args.user,
            "INSIGHT_STORAGE_ROOT": str(clone_storage),
        }
        api_result = run(
            [
                "uv", "run", "--directory", str(BACKEND), "--env-file", "../.env.local",
                "python", str(ROOT / "scripts" / "probe_restored_api.py"),
                "--credentials-file", str(credential_file), "--gone-import-batch", fixture["batch_id_for_410"],
            ],
            env=probe_env,
        )
        result_record = {
            "status": "passed",
            "run_id": run_id,
            "source_database": args.source_database,
            "disposable_clone_database": target,
            "global_retention_days": 180,
            "source_contract_days_for_fixture": 180,
            "dry_run": dry_summary,
            "cleanup": cleanup_summary,
            "fixture": fixture,
            "verification": verification,
            "api_probe": json.loads(api_result.stdout),
            "scope_note": "Cleanup ran only on a disposable template clone of the isolated synthetic acceptance DB. The clone DB is dropped; the local result and cloned storage evidence stay under .local/retention-drills.",
        }
        (output / "result.json").write_text(json.dumps(result_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result_record, ensure_ascii=False, indent=2))
    except Exception as exc:
        result_record.update({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)})
        (output / "result.json").write_text(json.dumps(result_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise
    finally:
        if created:
            run(compose + ["dropdb", "-U", args.user, target])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
