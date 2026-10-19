#!/usr/bin/env python3
"""Resumable release evidence ledger. Defaults never deploy or call a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUSES = {"passed", "failed", "not_run", "blocked_external", "not_applicable"}
TASKS = {
    "inventory": (1, None, "input lineage and frozen/source fingerprints"),
    "validate04": (2, None, "04 complete input contract validation"),
    "input_audit": (2, None, "02/04 grain mapping and 03/07 frozen/leakage inventory"),
    "originals01": (2, None, "01 controlled original import and original text/reference retrieval"),
    "current_demo": (2, None, "explicit current site-date 05/06 version, real import/actions/browser"),
    "data04": (2, None, "04 full asynchronous API import and independent persistence/scan oracle"),
    "data05": (2, None, "05 full asynchronous import and independent persistence/scan oracle"),
    "lifecycle04": (2, None, "04 invalid/empty/duplicate/revision/return/retraction lifecycle"),
    "actions06": (2, None, "05 gap/closed-event recurrence and 06 six action outcomes"),
    "package_browser": (2, None, "browser assertions against this run imported objects"),
    "frac": (3, None, "FR01-13 and AC01-26 expected/actual/evidence matrix"),
    "default_quality": (4, None, "baseline quality thresholds; fixture cannot satisfy model gates"),
    "real_quality": (4, "E2", "approved real models, independent labels and same-trace adjudication"),
    "real_sources": (5, "E1", "nine real business sources and financial signoff"),
    "preflight": (6, None, "offline deployment configuration; separate from target security"),
    "security": (6, None, "production imported modules, dependency/secret and security regression"),
    "integration": (6, None, "stable source backend/data/frontend/browser integration"),
    "target_security": (6, "E3", "real TLS/network/storage/accounts and deployment signoff"),
    "capacity": (7, "E3", "8 cores 16GiB SSD, 10 accounts, real business P95 and semantic scan"),
    "local_recovery": (8, None, "isolated faults, nonempty joint backup restore, retention and rollback preparation"),
    "target_recovery": (8, "E3", "measured business RPO/RTO, previous digest rollback, oncall signoff"),
    "pilot": (9, "E4", "three weeks, two operators, ten ASINs, thirty real checked events"),
    "candidate": (10, None, "reproducible source/lock/image/migration/config/rollback candidate"),
}


def now():
    return datetime.now(UTC).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_fingerprint():
    files = []
    for directory in ["backend/app", "backend/config", "backend/insight", "backend/tests_v2", "backend/tests_integration", "frontend/src", "frontend/e2e", "scripts", "dataset/tests"]:
        files.extend(p for p in (ROOT / directory).rglob("*") if p.is_file() and p.suffix in {".py", ".ts", ".tsx", ".css", ".sh"} and "__pycache__" not in p.parts)
    files.extend((ROOT / "dataset").glob("*.py"))
    files.extend(ROOT / p for p in ["frontend/vite.config.ts", "frontend/playwright.config.ts", "backend/pyproject.toml", "backend/uv.lock", "frontend/package.json", "frontend/pnpm-lock.yaml", "compose.v2.yml"])
    hashes = {str(p.relative_to(ROOT)): digest(p) for p in sorted(files) if p.is_file()}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(), hashes


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def initialize(run_dir, as_of):
    path = run_dir / "release_acceptance_manifest.json"
    code_sha, code_files = source_fingerprint()
    if path.exists():
        result = json.loads(path.read_text())
        for name, (phase, external, description) in TASKS.items():
            result["tasks"].setdefault(name, {"phase": phase, "required": True, "status": "blocked_external" if external else "not_run", "external_input": external, "owner": None, "expected": description, "actual": None, "attempts": [], "evidence": []})
        if result["as_of"] != as_of:
            raise ValueError("Resume must retain the original as_of; use a new run directory.")
        if result["source_sha256"] != code_sha:
            # Retain old evidence but prevent it from being accepted for this new source.
            result.setdefault("source_history", []).append({"source_sha256": result["source_sha256"], "changed_at": now()})
            for item in result["tasks"].values():
                if item["status"] == "passed":
                    item["status"] = "not_run"
                    item["stale_reason"] = "source_changed; previous attempts retained"
            result["source_sha256"] = code_sha
        save(run_dir / "source_hashes.json", code_files)
        save(path, result)
        return result
    result = {"schema_version": "release-acceptance-manifest-v1", "run_id": run_dir.name, "created_at": now(), "as_of": as_of,
              "actual_execution_time": now(), "marketplace_timezone": "America/Los_Angeles", "source_sha256": code_sha,
              "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "source_state": "shared_uncommitted; source_hashes bind actual files", "environment": "local_isolated; target not provided",
              "tasks": {name: {"phase": phase, "required": True, "status": "blocked_external" if external else "not_run",
                                "external_input": external, "owner": None, "expected": description, "actual": None,
                                "attempts": [], "evidence": []} for name, (phase, external, description) in TASKS.items()}}
    save(run_dir / "source_hashes.json", code_files)
    save(path, result)
    return result


def execute_step(manifest, manifest_path, task, command, *, cwd=ROOT, env=None, expected_exit=0):
    item = manifest["tasks"][task]
    attempt = {"idempotency_key": hashlib.sha256(json.dumps([manifest["run_id"], task, command, manifest["source_sha256"]]).encode()).hexdigest(),
               "started_at": now(), "command": command, "expected_exit": expected_exit, "source_sha256": manifest["source_sha256"]}
    ordinal = len(item["attempts"]) + 1
    logfile = manifest_path.parent / f"{task}-attempt-{ordinal}.log"
    item["attempts"].append(attempt)
    item["status"] = "not_run"
    save(manifest_path, manifest)
    with logfile.open("w") as output:
        process = subprocess.run(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, check=False)
    attempt.update(exit_code=process.returncode, finished_at=now(), log=str(logfile), log_sha256=digest(logfile))
    item["status"] = "passed" if process.returncode == expected_exit else "failed"
    item["actual"] = {"exit_code": process.returncode}
    item["evidence"] = [{"path": str(logfile), "sha256": digest(logfile)}]
    save(manifest_path, manifest)
    return process.returncode


def inventory(run_dir):
    packs = []
    for directory in sorted((ROOT / "dataset/data").iterdir()):
        if not directory.is_dir():
            continue
        files = [p for p in directory.rglob("*") if p.is_file() and "validation" not in p.parts and "__pycache__" not in p.parts]
        packs.append({"path": str(directory.relative_to(ROOT)), "source_nature": "real_original_read_only" if directory.name.startswith("01_") else "curated_labels_not_independently_signed" if directory.name.startswith("03_") else "synthetic_or_derived_acceptance",
                      "files": {str(p.relative_to(ROOT)): digest(p) for p in sorted(files)}})
    save(run_dir / "data_inventory.json", {"generated_at": now(), "packs": packs, "no_input_modified": True})


def verified_assessment(task, source_sha, task_name=None):
    """Logs prove execution only; product gates require a bound typed assessment."""
    assessment = task.get("assessment")
    if not assessment:
        return "missing_product_assessment"
    path = Path(assessment["path"])
    if not path.is_file() or digest(path) != assessment["sha256"]:
        return "invalid_product_assessment"
    try:
        report = json.loads(path.read_text())
    except (ValueError, OSError):
        return "invalid_product_assessment"
    if report.get("source_sha256") != source_sha or report.get("status") != "passed":
        return "stale_or_failed_product_assessment"
    checks = report.get("checks")
    if not isinstance(checks, dict) or not checks or any(value is not True for value in checks.values()):
        return "incomplete_product_checks"
    if task.get("external_input") and (report.get("source_nature") != "real" or not report.get("signoff", {}).get("reviewer") or not report.get("signoff", {}).get("signed_at")):
        return "missing_real_signed_assessment"
    metrics = report.get("metrics", {})
    minimums = {
        "real_quality": {"intent_accuracy": .95, "topic_macro_f1": .85, "polarity_macro_f1": .85, "test_recall_at_10": .85, "citation_validity": 1, "tool_difference_pairs": 24, "evidence_driven_pairs": 23, "independent_adjudicated_fields": 384},
        "default_quality": {"test_recall_at_10": .85, "citation_validity": 1, "tool_difference_pairs": 24, "evidence_driven_pairs": 23},
        "capacity": {"target_cpu_cores": 8, "target_memory_gib": 16, "unique_asins": 1000, "site_days": 180, "feedback_transaction_rows": 100000, "concurrent_distinct_users": 10},
        "pilot": {"shadow_days": 21, "operators": 2, "real_asins": 10, "checked_real_events": 30, "active_time_median_reduction": .30, "actionable_reports_fraction": .60},
    }
    maxima = {"real_quality": {"unsafe_outputs": 0}, "capacity": {"semantic_scan_seconds": 900, "cached_queue_p95_seconds": 3, "cached_report_p95_seconds": 3}, "pilot": {"general_invalid_alert_fraction": .20}}
    try:
        if any(isinstance(metrics.get(key), bool) or float(metrics[key]) < value for key, value in minimums.get(task_name, {}).items()):
            return "product_threshold_failed"
        if any(isinstance(metrics.get(key), bool) or not 0 <= float(metrics[key]) <= value for key, value in maxima.get(task_name, {}).items()):
            return "product_threshold_failed"
        if any(not math.isfinite(float(metrics[key])) for key in set(minimums.get(task_name, {})) | set(maxima.get(task_name, {}))):
            return "invalid_product_metrics"
    except (KeyError, TypeError, ValueError):
        return "missing_product_metrics"
    return None


def gate(manifest, run_dir):
    failures = []
    for name, task in manifest["tasks"].items():
        if task["status"] != "passed":
            failures.append({"task": name, "status": task["status"], "external_input": task["external_input"]})
        elif not task["evidence"] or any(not Path(e["path"]).is_file() or digest(e["path"]) != e["sha256"] for e in task["evidence"]):
            failures.append({"task": name, "status": "invalid_or_missing_evidence"})
        elif task["external_input"] and not task.get("business_signoff"):
            failures.append({"task": name, "status": "missing_external_signoff"})
        elif name not in {"inventory", "validate04"} and (reason := verified_assessment(task, manifest["source_sha256"], name)):
            failures.append({"task": name, "status": reason})
    result = {"schema_version": "full-product-release-gate-v1", "status": "release_ready" if not failures else "not_ready",
              "source_sha256": manifest["source_sha256"], "generated_at": now(), "manifest_sha256": digest(run_dir / "release_acceptance_manifest.json"),
              "failures": failures, "scope": "all mandatory product gates; no production deployment authorization"}
    save(run_dir / "release_gate.json", result)
    return 0 if not failures else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--stage", choices=["inventory", "validate04", "preflight", "data04", "data05", "lifecycle04", "actions06", "package_browser", "input_audit", "originals01", "oracles", "browser04", "browser05", "reconcile", "gate"], required=True)
    parser.add_argument("--pack-dir", type=Path)
    parser.add_argument("--business-as-of", help="explicit package replay date; may differ from overall run date")
    args = parser.parse_args()
    date.fromisoformat(args.as_of)
    run_dir = (args.run_dir if args.run_dir.is_absolute() else ROOT / args.run_dir).resolve()
    if not run_dir.is_relative_to(ROOT / ".local/acceptance-artifacts"):
        parser.error("run-dir must be under repository .local/acceptance-artifacts")
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = initialize(run_dir, args.as_of)
    path = run_dir / "release_acceptance_manifest.json"
    if args.stage == "reconcile":
        return subprocess.run([sys.executable, str(ROOT/"scripts/reconcile_release_evidence.py"), "--run-dir", str(run_dir)], cwd=ROOT, check=False).returncode
    if args.stage == "input_audit":
        return subprocess.run([sys.executable, str(ROOT/"scripts/audit_release_inputs.py"), "--output", str(run_dir/"input-provenance-audit.json")], cwd=ROOT, check=False).returncode
    if args.stage == "oracles":
        results = [subprocess.run([sys.executable, str(ROOT/"scripts/verify_package_metric_oracles.py"), "--output", str(run_dir/f"package-{pack}"), "--pack", pack], cwd=ROOT, check=False).returncode for pack in ["04", "05"]]
        return 0 if not any(results) else 1
    if args.stage in {"originals01", "browser04", "browser05"}:
        if not args.business_as_of:
            parser.error("explicit --business-as-of required for package replay")
        pack = "05" if args.stage == "browser05" else "04"
        command = [sys.executable, str(ROOT/"scripts/run_business_package_acceptance.py"), "--run-dir", str(run_dir), "--as-of", args.business_as_of, "--pack", pack, "--phase", "originals" if args.stage == "originals01" else "browser"]
        return subprocess.run(command, cwd=ROOT, check=False).returncode
    if args.stage == "gate":
        return gate(manifest, run_dir)
    if args.stage == "inventory":
        inventory(run_dir)
        report = run_dir / "data_inventory.json"
        manifest["tasks"]["inventory"].update(status="passed", actual={"pack_count": len(json.loads(report.read_text())["packs"])}, evidence=[{"path": str(report), "sha256": digest(report)}])
        save(path, manifest)
        return 0
    if args.stage == "validate04":
        pack = ((args.pack_dir if args.pack_dir.is_absolute() else ROOT / args.pack_dir) if args.pack_dir else ROOT / "dataset/data/04_经营事实增强").resolve()
        return execute_step(manifest, path, args.stage, [sys.executable, str(ROOT / "dataset/validate_business_facts.py"), "--data-dir", str(pack), "--report", str(run_dir / "04-validation.json")])
    if args.stage == "preflight":
        return execute_step(manifest, path, args.stage, [sys.executable, str(ROOT / "scripts/production_preflight.py"), "--json", "--broker-network", "host"])
    command = [sys.executable, str(ROOT / "scripts/run_business_package_acceptance.py"), "--run-dir", str(run_dir), "--as-of", args.business_as_of or args.as_of, "--pack", "04" if args.stage in {"data04", "lifecycle04"} else "05"]
    if args.stage in {"lifecycle04", "actions06", "package_browser"}:
        command.extend(["--phase", {"lifecycle04": "lifecycle", "actions06": "actions", "package_browser": "browser"}[args.stage]])
    if args.pack_dir:
        command.extend(["--pack-dir", str((args.pack_dir if args.pack_dir.is_absolute() else ROOT / args.pack_dir).resolve())])
    return execute_step(manifest, path, args.stage, command)


if __name__ == "__main__":
    raise SystemExit(main())
