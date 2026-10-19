import importlib.util
import json
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("release_acceptance", Path(__file__).resolve().parents[2] / "scripts/release_acceptance.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_failed_step_can_resume_without_erasing_failed_attempt(tmp_path):
    manifest = MODULE.initialize(tmp_path, "2023-09-03")
    path = tmp_path / "release_acceptance_manifest.json"
    assert MODULE.execute_step(manifest, path, "validate04", [sys.executable, "-c", "raise SystemExit(3)"]) == 3
    assert manifest["tasks"]["validate04"]["status"] == "failed"
    assert MODULE.execute_step(manifest, path, "validate04", [sys.executable, "-c", "print('controlled success')"]) == 0
    attempts = json.loads(path.read_text())["tasks"]["validate04"]["attempts"]
    assert [item["exit_code"] for item in attempts] == [3, 0]
    assert MODULE.gate(manifest, tmp_path) == 1


def test_empty_or_tampered_evidence_never_releases(tmp_path):
    manifest = MODULE.initialize(tmp_path, "2023-09-03")
    for task in manifest["tasks"].values():
        task["status"] = "passed"
    MODULE.save(tmp_path / "release_acceptance_manifest.json", manifest)
    assert MODULE.gate(manifest, tmp_path) == 1
    assert json.loads((tmp_path / "release_gate.json").read_text())["status"] == "not_ready"


def test_successful_logs_cannot_replace_product_thresholds(tmp_path):
    manifest = MODULE.initialize(tmp_path, "2023-09-03")
    log = tmp_path / "success.log"
    log.write_text("exit 0")
    for task in manifest["tasks"].values():
        task.update(status="passed", evidence=[{"path": str(log), "sha256": MODULE.digest(log)}], business_signoff=True)
    MODULE.save(tmp_path / "release_acceptance_manifest.json", manifest)
    assert MODULE.gate(manifest, tmp_path) == 1
    failures = json.loads((tmp_path / "release_gate.json").read_text())["failures"]
    assert any(row["task"] == "real_quality" and row["status"] == "missing_product_assessment" for row in failures)


def test_synthetic_or_stale_assessment_cannot_satisfy_real_gate(tmp_path):
    path = tmp_path / "assessment.json"
    report = {"source_sha256": "current", "status": "passed", "checks": {"thresholds": True}, "source_nature": "synthetic"}
    path.write_text(json.dumps(report))
    task = {"assessment": {"path": str(path), "sha256": MODULE.digest(path)}, "external_input": "E2"}
    assert MODULE.verified_assessment(task, "current") == "missing_real_signed_assessment"
    assert MODULE.verified_assessment(task, "old") == "stale_or_failed_product_assessment"


def test_quality_cannot_pass_with_all_true_checks_but_bad_or_nan_metrics(tmp_path):
    path = tmp_path / "assessment.json"
    report = {"source_sha256": "current", "status": "passed", "checks": {"thresholds": True}, "metrics": {"test_recall_at_10": .125, "citation_validity": 1, "tool_difference_pairs": 2, "evidence_driven_pairs": 1}}
    path.write_text(json.dumps(report))
    task = {"assessment": {"path": str(path), "sha256": MODULE.digest(path)}, "external_input": None}
    assert MODULE.verified_assessment(task, "current", "default_quality") == "product_threshold_failed"
    report["metrics"].update(test_recall_at_10=float("nan"), tool_difference_pairs=24, evidence_driven_pairs=23)
    path.write_text(json.dumps(report))
    task["assessment"]["sha256"] = MODULE.digest(path)
    assert MODULE.verified_assessment(task, "current", "default_quality") == "invalid_product_metrics"
