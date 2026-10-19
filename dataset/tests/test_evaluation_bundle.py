"""CLI chain consumes actual adjudications, preserves provenance and rejects mismatch."""
import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from evaluation_bundle import assemble, digest, file_hash  # noqa: E402
from insight.investigation.evaluation import BUSINESS_SCHEMA_VERSION, SEMANTIC_FIELDS, trace_hash, write_template  # noqa: E402


def run(script, *args):
    return subprocess.run([sys.executable, str(ROOT / script), *map(str, args)], capture_output=True, text=True)


def build(tmp_path):
    runtime = tmp_path / "runtime.json"
    traces = [{"scenario_id": f"independent-{i}", "status": "completed", "tools": [],
               "result": {"output_schema_version": "investigation-output-v1"}} for i in range(2)]
    runtime.write_text(json.dumps({"trajectories": traces, "pending_adjudication_field_count": 16,
                                  "output_schema_version": "investigation-output-v1", "request_count": 2, "pair_count": 1}))
    judgments = tmp_path / "judgments.csv"
    write_template(judgments, traces)
    rows = list(csv.DictReader(judgments.open()))
    decisions = []
    for i, row in enumerate(rows):
        row.update(reviewer="controlled-independent-example", reviewed_at="2026-10-07T00:00:00Z", adjudication_kind="controlled_fixture")
        values = {field: "retain_unknown" if i else "cite_supported_evidence" for field in SEMANTIC_FIELDS}
        values.update(expected_route="retrieve_support", expected_tool_names='["retrieve_support"]', safety_escalation="none")
        row.update(values)
        decisions.append(values)
    with judgments.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
    adapted = tmp_path / "adapted.json"
    result = run("adapt_investigation_outputs.py", "--runtime-report", runtime, "--adjudications", judgments, "--output", adapted)
    assert result.returncode == 0, result.stderr
    pack = tmp_path / "pack"; pack.mkdir()
    gold = [{"scenario_id": trace["scenario_id"], "pair_id": "development-pair", "evidence_variant": str(i), **decisions[i]} for i, trace in enumerate(traces)]
    with (pack / "paired_investigation_scenarios.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=gold[0].keys()); writer.writeheader(); writer.writerows(gold)
    score = tmp_path / "score.json"
    result = run("score_paired_investigations.py", "--runtime-report", runtime, "--adapted", adapted, "--pack-dir", pack, "--output", score)
    assert result.returncode == 0, result.stderr
    return runtime, adapted, score


def test_complete_cli_chain_consumes_new_decisions_and_retains_fixture_origin(tmp_path):
    runtime, adapted, score = build(tmp_path)
    bundle = assemble(runtime, adapted, score)
    assert bundle["pending_adjudication_field_count"] == 0  # raw runtime still says 16
    assert bundle["scoring"]["status"] == "passed"
    assert bundle["adjudication_origins"] == ["controlled_fixture"]
    assert bundle["independent_adjudication_complete"] is False
    retrieval = tmp_path / "retrieval.json"; retrieval.write_text('{}')
    out, assembled = tmp_path / "gate.json", tmp_path / "bundle.json"
    result = run("quality_gate.py", "--retrieval", retrieval, "--paired", runtime, "--adapted", adapted,
                 "--scoring", score, "--bundle-output", assembled, "--output", out)
    assert result.returncode == 1  # fixture and tiny sample never establish release quality
    assert json.loads(assembled.read_text())["pending_adjudication_field_count"] == 0
    assert not any("pending_adjudication_field_count" in failure for failure in json.loads(out.read_text())["failures"])


@pytest.mark.parametrize("fault", ["old_runtime", "missing_scenario", "old_trace", "wrong_origin", "modified_score"])
def test_evaluation_bundle_rejects_mismatched_or_lost_provenance(tmp_path, fault):
    runtime, adapted, score = build(tmp_path)
    if fault == "old_runtime":
        data = json.loads(runtime.read_text()); data["new_run"] = True; runtime.write_text(json.dumps(data))
    elif fault == "modified_score":
        data = json.loads(score.read_text()); data["status"] = "failed"; score.write_text(json.dumps(data))
    else:
        data = json.loads(adapted.read_text())
        if fault == "missing_scenario":
            data["predictions"].pop()
        elif fault == "old_trace":
            data["provenance"][0]["trace_sha256"] = "old"
        else:
            data["provenance"][0]["adjudication"]["adjudication_kind"] = "unverified"
        adapted.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        assemble(runtime, adapted, score)
