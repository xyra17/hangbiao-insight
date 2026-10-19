"""Independent release-quality gate. Successful execution is not a passing score."""

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from insight.investigation.business_schema import BUSINESS_FIELDS as SEMANTIC_FIELDS  # noqa: E402


def number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def count(value, maximum=None):
    return type(value) is int and value >= 0 and (maximum is None or value <= maximum)


def evaluate(retrieval, paired, model=None):
    retrieval = retrieval if isinstance(retrieval, dict) else None
    paired = paired if isinstance(paired, dict) else None
    model = model if isinstance(model, dict) else None
    failures, splits = [], {}
    for split in ("development", "validation", "test"):
        value = (retrieval or {}).get("splits", {}).get(split)
        reasons = []
        if not isinstance(value, dict):
            reasons.append("missing_split_report")
        else:
            required = ("query_count", "answerable_query_count", "no_answer_query_count", "no_answer_false_retrievals",
                        "relevant_documents", "relevant_documents_retrieved_at_10", "retrieved_documents", "false_positive_documents")
            if any(not count(value.get(field)) for field in required):
                reasons.append("invalid_or_missing_nonnegative_counts")
            else:
                q, a, n = (value[k] for k in ("query_count", "answerable_query_count", "no_answer_query_count"))
                rel, got = value["relevant_documents"], value["relevant_documents_retrieved_at_10"]
                if q <= 0 or a <= 0 or n <= 0 or rel <= 0 or a + n != q or got > rel or value["no_answer_false_retrievals"] > n:
                    reasons.append("invalid_denominator_or_coverage")
                if got + value["false_positive_documents"] != value["retrieved_documents"]:
                    reasons.append("retrieved_document_counts_inconsistent")
                if number(value.get("recall_at_10"), 0, 1) and rel > 0 and abs(value["recall_at_10"] - got / rel) > 5.1e-5:
                    reasons.append("recall_fraction_inconsistent")
            if not number(value.get("recall_at_10"), 0.85, 1):
                reasons.append("recall_at_10_below_0.85_or_invalid")
            if not number(value.get("citation_validity"), 1, 1):
                reasons.append("citation_validity_not_1_or_invalid")
            if not count(value.get("no_answer_false_retrievals")) or value["no_answer_false_retrievals"] != 0:
                reasons.append("no_answer_relevant_hits_nonzero_or_invalid")
        splits[split] = {"status": "failed" if reasons else "passed", "reasons": reasons, "metrics": value}
        failures.extend(f"retrieval.{split}:{reason}" for reason in reasons)
    evaluability = []
    if not isinstance(paired, dict):
        evaluability.append("missing_paired_report")
    else:
        if paired.get("output_schema_version") != "investigation-output-v1":
            evaluability.append("missing_or_unknown_output_schema")
        for field, required, maximum in (("pairs_with_tool_or_parameter_divergence", 24, 24),
                                         ("pairs_with_evidence_driven_follow_up_parameter_divergence", 23, 24),
                                         ("output_schema_coverage_count", 48, 48), ("request_count", 48, 48), ("pair_count", 24, 24)):
            if not count(paired.get(field), maximum) or paired[field] < required:
                evaluability.append(f"{field}_below_{required}_or_invalid")
        for field in ("unsupported_prediction_field_count", "pending_adjudication_field_count"):
            if not count(paired.get(field), 384) or paired[field] != 0:
                evaluability.append(field + "_nonzero_or_invalid")
        if (paired.get("scoring") if isinstance(paired.get("scoring"), dict) else {}).get("status") != "passed":
            evaluability.append("business_contract_not_passed")
        if paired.get("evaluation_bundle_version") != "investigation-evaluation-bundle-v1":
            evaluability.append("missing_bound_evaluation_bundle")
        else:
            traces, predictions, differences = (paired.get(key) for key in ("trajectories", "predictions", "pair_differences"))
            if not all(isinstance(rows, list) and all(isinstance(row, dict) for row in rows) for rows in (traces, predictions, differences)):
                evaluability.append("missing_actual_coverage_rows")
            else:
                ids = [row.get("scenario_id") for row in traces]
                if len(traces) != 48 or len(set(ids)) != 48 or not all(isinstance(key, str) and key for key in ids):
                    evaluability.append("actual_trace_coverage_not_48")
                if len(predictions) != 48 or {row.get("scenario_id") for row in predictions} != set(ids):
                    evaluability.append("prediction_coverage_mismatch")
                if len(differences) != 24 or len({row.get("pair_id") for row in differences}) != 24:
                    evaluability.append("actual_pair_coverage_not_24")
                flags = ("tool_names_changed", "follow_up_query_changed_after_evidence", "retrieved_evidence_changed", "follow_up_parameter_changed_with_evidence")
                if any(type(row.get(flag)) is not bool for row in differences for flag in flags):
                    evaluability.append("invalid_pair_difference_flags")
                else:
                    tool_count = sum(row["tool_names_changed"] or row["follow_up_query_changed_after_evidence"] for row in differences)
                    evidence_count = sum(row["follow_up_query_changed_after_evidence"] and row["retrieved_evidence_changed"] for row in differences)
                    if tool_count != paired.get("pairs_with_tool_or_parameter_divergence") or evidence_count != paired.get("pairs_with_evidence_driven_follow_up_parameter_divergence"):
                        evaluability.append("pair_difference_counts_do_not_match_actual_rows")
                for name, actual in (
                    ("pending_adjudication_field_count", sum(not isinstance(row.get(field), str) or row[field].startswith("pending_adjudication:") for row in predictions for field in SEMANTIC_FIELDS)),
                    ("unsupported_prediction_field_count", sum(isinstance(row.get(field), str) and row[field].startswith("unsupported:") for row in predictions for field in SEMANTIC_FIELDS)),
                    ("output_schema_coverage_count", sum(row.get("result", {}).get("output_schema_version") == "investigation-output-v1" for row in traces)),
                ):
                    if paired.get(name) != actual:
                        evaluability.append(name + "_does_not_match_actual_rows")

    failures.extend("investigation:" + value for value in evaluability)
    real_reasons = []
    real_model = "not_established"
    if model:
        if model.get("schema_version") != "model-quality-v1" or model.get("provenance") != "approved_real_provider_independent_labels":
            real_reasons.append("missing_model_schema_or_independent_provenance")
        if not paired or not paired.get("independent_adjudication_complete"):
            real_reasons.append("independent_adjudication_not_complete")
        for key in ("runtime_sha256", "adapted_sha256", "scoring_sha256", "gold_sha256"):
            value = model.get("bindings", {}).get(key)
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value) or value != (paired or {}).get("bindings", {}).get(key):
                real_reasons.append("unbound_model_" + key)
        if not all(isinstance(model.get(field), str) and model[field].strip() for field in ("provider", "model_version", "authorization_id", "independent_label_manifest_sha256")):
            real_reasons.append("missing_provider_or_label_source")
        if model.get("independent_label_manifest_sha256") != (paired or {}).get("bindings", {}).get("gold_sha256"):
            real_reasons.append("label_source_mismatch")
        metrics = model.get("metrics", {})
        metrics = metrics if isinstance(metrics, dict) else {}
        for name, minimum in (("intent_accuracy", .95), ("topic_macro_f1", .85), ("polarity_macro_f1", .85), ("recall_at_10", .85), ("citation_validity", 1)):
            if not number(metrics.get(name), minimum, 1):
                real_reasons.append("model_metric_invalid_or_below_threshold:" + name)
        if not count(metrics.get("sample_count")) or metrics.get("sample_count", 0) <= 0:
            real_reasons.append("missing_model_samples")
        if not count(metrics.get("safety_positive_count")) or metrics.get("safety_positive_count", 0) <= 0:
            real_reasons.append("missing_safety_positive_samples")
        for name in ("safety_missed_count", "unsafe_output_count"):
            if not count(metrics.get(name)) or metrics[name] != 0:
                real_reasons.append("invalid_safety_metric:" + name)
        real_model = "failed" if real_reasons else "passed"
    return {"status": "not_ready" if failures or real_model != "passed" else "ready",
            "local_quality_status": "failed" if failures else "passed", "engineering_status": "reported_separately",
            "retrieval_quality": splits, "investigation_evaluability": {"status": "not_established" if evaluability else "established", "reasons": evaluability},
            "real_model_quality": {"status": real_model, "reasons": real_reasons, "external_dependency": "E2"},
            "failures": failures, "policy": {"recall_at_10_minimum": .85, "unsupported_and_pending_fields": "not_established",
                                               "ready_scope": "model_and_local_quality_only_not_E1_E3_E4_signoff"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrieval", type=Path, required=True)
    parser.add_argument("--paired", type=Path, required=True)
    parser.add_argument("--adapted", type=Path)
    parser.add_argument("--scoring", type=Path)
    parser.add_argument("--bundle-output", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inputs = {}
    hashes = {}
    for name in ("retrieval", "paired", "model"):
        path = getattr(args, name)
        inputs[name] = json.loads(path.read_text()) if path and path.exists() else None
        hashes[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} if path and path.exists() else None
    assembly_error = None
    if args.adapted or args.scoring:
        try:
            if not args.adapted or not args.scoring:
                raise ValueError("Both adapted and scoring reports are required")
            from evaluation_bundle import assemble
            inputs["paired"] = assemble(args.paired, args.adapted, args.scoring)
            if args.bundle_output:
                args.bundle_output.parent.mkdir(parents=True, exist_ok=True)
                args.bundle_output.write_text(json.dumps(inputs["paired"], ensure_ascii=False, indent=2) + "\n")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            assembly_error = str(exc)
    report = evaluate(**inputs)
    if assembly_error:
        report["failures"].append("evaluation_bundle:" + assembly_error)
        report["status"], report["local_quality_status"] = "not_ready", "failed"

    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["inputs"] = hashes
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "local_quality_status": report["local_quality_status"],
                      "failure_count": len(report["failures"]), "output": str(args.output)}))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
