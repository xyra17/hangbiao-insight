"""Bind runtime, adapted decisions and scorer output without rewriting runtime evidence."""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from insight.investigation.evaluation import ADAPTER_VERSION, SEMANTIC_FIELDS, adapt_prediction, trace_hash  # noqa: E402
from insight.investigation.business_schema import BUSINESS_SCHEMA_VERSION  # noqa: E402


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_adapted(runtime, adapted, runtime_hash):
    traces = runtime.get("trajectories", [])
    ids = [row["scenario_id"] for row in traces]
    predictions = adapted.get("predictions", [])
    metadata = adapted.get("provenance", [])
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("runtime scenario coverage is empty or duplicated")
    if adapted.get("adapter_version") != ADAPTER_VERSION or adapted.get("runtime_report_sha256") != runtime_hash:
        raise ValueError("adapted report belongs to an old runtime or adapter")
    if adapted.get("scenario_ids") != sorted(ids):
        raise ValueError("adapted scenario set differs from runtime")
    for rows in (predictions, metadata):
        if len(rows) != len(ids) or {row.get("scenario_id") for row in rows} != set(ids):
            raise ValueError("adapted coverage is missing or duplicated")
    by_id = {row["scenario_id"]: row for row in metadata}
    by_prediction = {row["scenario_id"]: row for row in predictions}
    for trace in traces:
        meta = by_id[trace["scenario_id"]]
        if meta.get("trace_sha256") != trace_hash(trace) or meta.get("business_schema_version") != BUSINESS_SCHEMA_VERSION:
            raise ValueError("trace hash or business schema mismatch")
        judgment = meta.get("adjudication")
        if judgment:
            from datetime import datetime
            if judgment.get("business_schema_version") != BUSINESS_SCHEMA_VERSION:
                raise ValueError("adjudication schema mismatch")
            if judgment.get("adjudication_kind") not in {"controlled_fixture", "independent_human"}:
                raise ValueError("unrecognized adjudication provenance")
            if not judgment.get("reviewer") or datetime.fromisoformat(judgment.get("reviewed_at", "").replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("adjudication lacks reviewer or zoned timestamp")
            if judgment.get("trace_sha256") != trace_hash(trace) or judgment.get("scenario_id") != trace["scenario_id"]:
                raise ValueError("adjudication is bound to an old trace")
        expected, expected_meta = adapt_prediction(trace, judgment)
        if expected != by_prediction[trace["scenario_id"]] or expected_meta["field_statuses"] != meta.get("field_statuses"):
            raise ValueError("prediction or field provenance does not match actual output/adjudication")
    return predictions


def assemble(runtime_path, adapted_path, scoring_path):
    runtime = json.loads(Path(runtime_path).read_text())
    adapted = json.loads(Path(adapted_path).read_text())
    score = json.loads(Path(scoring_path).read_text())
    predictions = validate_adapted(runtime, adapted, file_hash(runtime_path))
    if score.get("business_schema_version") != BUSINESS_SCHEMA_VERSION:
        raise ValueError("scorer uses a different business schema")
    bindings = score.get("bindings", {})
    if bindings.get("runtime_sha256") != file_hash(runtime_path) or bindings.get("adapted_sha256") != file_hash(adapted_path):
        raise ValueError("scoring report belongs to a different runtime or adapted output")
    if bindings.get("predictions_sha256") != digest(predictions):
        raise ValueError("scorer predictions differ from adapted decisions")
    if bindings.get("scenario_ids") != adapted["scenario_ids"]:
        raise ValueError("scorer coverage differs from runtime")
    from score_paired_investigations import read_rows, score_rows
    gold = Path(bindings.get("gold_path", ""))
    if not gold.is_file() or file_hash(gold) != bindings.get("gold_sha256"):
        raise ValueError("scoring labels are missing or changed")
    reproduced = score_rows(read_rows(gold), predictions, mode=score["scoring_mode"])
    if reproduced["pairs"] != score.get("pairs") or reproduced["status"] != score.get("status"):
        raise ValueError("scorer result is inconsistent with bound predictions/labels")
    if {row["scenario_id"] for row in read_rows(gold)} != set(adapted["scenario_ids"]):
        raise ValueError("scorer labels do not cover the actual scenario set")
    traces = runtime["trajectories"]
    pending = sum(not row.get(field) or row[field].startswith("pending_adjudication:") for row in predictions for field in SEMANTIC_FIELDS)
    unsupported = sum(row.get(field, "").startswith("unsupported:") for row in predictions for field in SEMANTIC_FIELDS)
    origins = [meta.get("adjudication", {}).get("adjudication_kind") if meta.get("adjudication") else "model_output_or_pending" for meta in adapted["provenance"]]
    return {**runtime, "evaluation_bundle_version": "investigation-evaluation-bundle-v1",
            "pending_adjudication_field_count": pending, "unsupported_prediction_field_count": unsupported,
            "output_schema_coverage_count": sum(row.get("result", {}).get("output_schema_version") == "investigation-output-v1" for row in traces),
            "request_count": len(traces), "scoring": score, "predictions": predictions,
            "adjudication_origins": sorted(set(origins)),
            "independent_adjudication_complete": pending == 0 and all(origin == "independent_human" for origin in origins),
            "bindings": {"runtime_sha256": file_hash(runtime_path), "adapted_sha256": file_hash(adapted_path),
                         "scoring_sha256": file_hash(scoring_path), "gold_sha256": bindings["gold_sha256"],
                         "scenario_ids": adapted["scenario_ids"], "business_schema_version": BUSINESS_SCHEMA_VERSION}}
