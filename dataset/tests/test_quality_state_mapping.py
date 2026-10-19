"""Component states must propagate without bypassing external product gates."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from reconcile_release_evidence import quality_checks
from release_acceptance import digest, gate, initialize, save
from test_quality_gate import module, valid


def test_ready_component_propagates():
    report = module.evaluate(*valid())
    assert report["status"] == "ready"
    assert all(quality_checks(report).values())


def test_missing_model_does_not_fail_valid_local_component():
    retrieval, paired, _ = valid()
    report = module.evaluate(retrieval, paired)
    assert all(quality_checks(report).values())
    assert report["status"] == "not_ready"
    assert report["real_model_quality"]["status"] == "not_established"


def test_numeric_failure_and_pending_fields_remain_failed():
    retrieval, paired, model = valid()
    retrieval["splits"]["test"]["recall_at_10"] = .125
    assert not all(quality_checks(module.evaluate(retrieval, paired, model)).values())
    retrieval, paired, model = valid()
    paired["predictions"][0]["expected_next_step"] = "pending_adjudication:missing"
    paired["pending_adjudication_field_count"] = 1
    assert not all(quality_checks(module.evaluate(retrieval, paired, model)).values())


def test_wrong_status_or_forged_summary_cannot_propagate():
    report = module.evaluate(*valid())
    report["status"] = "release_ready"
    assert not all(quality_checks(report).values())
    report["status"] = "ready"
    report["retrieval_quality"]["test"]["status"] = "failed"
    assert not all(quality_checks(report).values())


def test_component_success_still_requires_real_model_and_other_signoffs(tmp_path):
    manifest = initialize(tmp_path, "2026-10-01")
    quality = module.evaluate(*valid())
    assessment = tmp_path / "local-assessment.json"
    save(assessment, {"status": "passed", "source_sha256": manifest["source_sha256"],
                      "checks": quality_checks(quality), "metrics": {
                          "test_recall_at_10": 1, "citation_validity": 1,
                          "tool_difference_pairs": 24, "evidence_driven_pairs": 23}})
    manifest["tasks"]["default_quality"].update(status="passed", evidence=[{"path": str(assessment), "sha256": digest(assessment)}], assessment={"path": str(assessment), "sha256": digest(assessment)})
    save(tmp_path / "release_acceptance_manifest.json", manifest)
    assert gate(manifest, tmp_path) == 1
    failures = json.loads((tmp_path / "release_gate.json").read_text())["failures"]
    assert not any(row["task"] == "default_quality" for row in failures)
    for name in ("real_quality", "real_sources", "target_security", "capacity", "target_recovery", "pilot"):
        assert any(row["task"] == name and row["status"] == "blocked_external" for row in failures)


def test_recomputed_quality_rejects_changed_inputs_and_forged_summary(tmp_path, monkeypatch):
    import pytest
    from reconcile_release_evidence import recompute_quality
    retrieval, paired, model = valid()
    report = module.evaluate(retrieval, paired, model)
    report['inputs'] = {}
    for name, value in [('retrieval', retrieval), ('paired', paired), ('model', model)]:
        path = tmp_path / (name + '.json')
        save(path, value)
        report['inputs'][name] = {'path': str(path), 'sha256': digest(path)}
    paired['bindings']['runtime_sha256'] = report['inputs']['paired']['sha256']
    model['bindings']['runtime_sha256'] = paired['bindings']['runtime_sha256']
    # Model and bundle must have the same actual runtime binding.
    save(tmp_path / 'model.json', model)
    report['inputs']['model']['sha256'] = digest(tmp_path / 'model.json')
    monkeypatch.setattr("reconcile_release_evidence.assemble", lambda *args: paired)
    save(tmp_path / 'quality-bundle-final.json', paired)
    save(tmp_path / 'quality-gate-final.json', report)
    assert recompute_quality(tmp_path)['status'] == 'ready'
    report['status'] = 'release_ready'
    save(tmp_path / 'quality-gate-final.json', report)
    with pytest.raises(ValueError, match='summary'):
        recompute_quality(tmp_path)
    report['status'] = 'ready'
    save(tmp_path / 'quality-gate-final.json', report)
    save(tmp_path / 'retrieval.json', {})
    with pytest.raises(ValueError, match='hash mismatch'):
        recompute_quality(tmp_path)
