import csv
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parents[2] / "backend"))
from insight.investigation import evaluation as adapter


def test_semantic_fields_remain_pending_without_independent_judgment():
    trace = {"scenario_id": "new", "status": "completed", "tools": [{"name": "text_evidence", "status": "completed"}], "result": {"conclusion": {"status": "unknown"}}}
    prediction, metadata = adapter.adapt_prediction(trace)
    assert all(prediction[field].startswith("pending_adjudication:") for field in adapter.SEMANTIC_FIELDS)
    assert metadata["raw_tool_names"] == ["text_evidence"]
    assert metadata["semantic_tool_names"] == ["search_feedback_evidence"]


@pytest.mark.parametrize("fault", ["missing_reviewer", "stale_trace", "duplicate", "unknown_id", "missing_judgment"])
def test_independent_judgment_import_rejects_unbound_or_incomplete_rows(tmp_path, fault):
    trace = {"scenario_id": "new", "result": {"conclusion": {"status": "unknown"}}}
    row = {"business_schema_version": adapter.BUSINESS_SCHEMA_VERSION, "scenario_id": "new", "trace_sha256": adapter.trace_hash(trace), "reviewer": "reviewer-1",
           "reviewed_at": "2026-10-06T00:00:00Z", "adjudication_kind": "independent_human",
           **{field: "unknown" for field in adapter.SEMANTIC_FIELDS}}
    if fault == "missing_reviewer":
        row["reviewer"] = ""
    elif fault == "stale_trace":
        row["trace_sha256"] = "stale"
    elif fault == "unknown_id":
        row["scenario_id"] = "other"
    elif fault == "missing_judgment":
        row["allowed_conclusion"] = ""
    path = tmp_path / "judgments.csv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=adapter.JUDGMENT_FIELDS)
        writer.writeheader()
        writer.writerow(row)
        if fault == "duplicate":
            writer.writerow(row)
    with pytest.raises(ValueError):
        adapter.load_adjudications(path, [trace])


def test_actual_trace_cli_exports_predictions_csv_without_gold(tmp_path):
    import json
    import subprocess
    import sys

    report = tmp_path / "runtime.json"
    report.write_text(json.dumps({"trajectories": [{"scenario_id": "independent-new", "status": "completed", "tools": [], "result": {}}]}))
    predictions = tmp_path / "predictions.csv"
    result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "adapt_investigation_outputs.py"),
                             "--runtime-report", str(report), "--predictions-csv", str(predictions),
                             "--write-template", str(tmp_path / "judgments.csv"), "--output", str(tmp_path / "adapted.json")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with predictions.open() as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["scenario_id"] == "independent-new"
    assert rows[0]["allowed_conclusion"] == "pending_adjudication:allowed_conclusion"
