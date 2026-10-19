#!/usr/bin/env python3
"""Score S4/S6 paired investigation decisions against the versioned contract."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "backend"))
from insight.investigation.business_schema import BUSINESS_FIELDS, BUSINESS_SCHEMA_VERSION  # noqa: E402
DEFAULT_PACK = ROOT / "data" / "07_S4_S6独立评测_v1"
FIELDS = BUSINESS_FIELDS

CONTRAST_FIELDS = (
    "expected_behavior",
    "expected_next_step",
    "allowed_conclusion",
    "forbidden_conclusion",
    "expected_stop_condition",
)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def normalize_field(field: str, value: str) -> str:
    value = value.strip()
    if field == "expected_tool_names":
        try:
            tools = json.loads(value)
        except json.JSONDecodeError:
            tools = [part.strip() for part in value.split("|") if part.strip()]
        if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
            raise ValueError("expected_tool_names must be a JSON array of strings")
        return json.dumps(sorted(set(tools)), ensure_ascii=False, separators=(",", ":"))
    return value


def group_rows(rows: list[dict[str, str]], key: str) -> dict[str, dict[str, dict[str, str]]]:
    grouped: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for row in rows:
        group_id = row.get("pair_id", "")
        variant_id = row.get(key, "")
        if not group_id or not variant_id:
            raise ValueError(f"Rows must include pair_id and {key}.")
        if variant_id in grouped[group_id]:
            raise ValueError(f"Duplicate {key} {variant_id}.")
        grouped[group_id][variant_id] = row
    return dict(grouped)


def score_rows(gold_rows: list[dict[str, str]], prediction_rows: list[dict[str, str]], *, mode: str) -> dict:
    gold = {row["scenario_id"]: row for row in gold_rows}
    predictions = {row.get("scenario_id", ""): row for row in prediction_rows}
    if "" in predictions:
        raise ValueError("Every prediction row needs a scenario_id.")
    duplicates = len(prediction_rows) != len(predictions)
    gold_pairs = group_rows(gold_rows, "scenario_id")
    extra_ids = sorted(set(predictions) - set(gold))
    scenario_results = {}

    for scenario_id, expected in gold.items():
        actual = predictions.get(scenario_id, {})
        field_matches = {
            field: (
                normalize_field(field, actual.get(field, ""))
                == normalize_field(field, expected.get(field, ""))
            )
            for field in FIELDS
        }
        pending = [field for field in FIELDS if not actual.get(field) or actual.get(field, "").startswith(("unsupported:", "pending_adjudication:"))]
        scenario_results[scenario_id] = {
            "status": "not_established" if pending else "passed" if all(field_matches.values()) else "failed",
            "unassessed_fields": pending,
            "matched_fields": sum(field_matches.values()),
            "field_count": len(FIELDS),
            "field_matches": field_matches,
            "expected": {field: expected.get(field, "") for field in FIELDS},
            "predicted": {field: actual.get(field, "") for field in FIELDS},
        }

    pair_results = []
    for pair_id, expected_rows in sorted(gold_pairs.items()):
        expected_variants = expected_rows
        expected_contrasts = []
        predicted_contrasts = []
        for field in CONTRAST_FIELDS:
            variants = list(expected_variants.values())
            if len({row.get(field, "") for row in variants}) > 1:
                expected_contrasts.append(field)
                predicted_values = [predictions.get(row["scenario_id"], {}).get(field, "") for row in variants]
                if len(set(predicted_values)) > 1:
                    predicted_contrasts.append(field)
        pair_rows = [scenario_results[row["scenario_id"]] for row in expected_variants.values()]
        safety_gate = None
        if pair_id == "PAIR-10":
            positive = next((row for row in expected_variants.values() if row["evidence_variant"] == "supports_claim"), {})
            negative = next((row for row in expected_variants.values() if row["evidence_variant"] == "changes_decision"), {})
            predicted_positive = predictions.get(positive.get("scenario_id", ""), {})
            predicted_negative = predictions.get(negative.get("scenario_id", ""), {})
            safety_gate = (
                predicted_positive.get("safety_escalation") == "urgent_handoff"
                and predicted_negative.get("safety_escalation") == "none"
            )
        pair_passed = (
            len(expected_variants) == 2
            and all(row["status"] == "passed" for row in pair_rows)
            and expected_contrasts
            and set(expected_contrasts) == set(predicted_contrasts)
            and safety_gate is not False
        )
        pair_results.append({
            "pair_id": pair_id,
            "status": "not_established" if any(row["status"] == "not_established" for row in pair_rows) else "passed" if pair_passed else "failed",
            "variants": [
                {
                    "scenario_id": row["scenario_id"],
                    "evidence_variant": row["evidence_variant"],
                    **scenario_results[row["scenario_id"]],
                }
                for row in sorted(expected_variants.values(), key=lambda item: item["scenario_id"])
            ],
            "expected_contrast_fields": expected_contrasts,
            "predicted_contrast_fields": predicted_contrasts,
            "pair_safety_gate_pass": safety_gate,
        })

    passed_pairs = sum(pair["status"] == "passed" for pair in pair_results)
    report = {
        "status": "not_established" if any(pair["status"] == "not_established" for pair in pair_results) else "passed" if passed_pairs == len(pair_results) and not extra_ids and not duplicates else "failed",
        "scoring_mode": mode,
        "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "pair_count": len(pair_results),
        "scenario_count": len(gold_rows),
        "pairs_passed": passed_pairs,
        "pairs_failed": sum(pair["status"] == "failed" for pair in pair_results),
        "pairs_not_established": sum(pair["status"] == "not_established" for pair in pair_results),
        "extra_prediction_scenario_ids": extra_ids,
        "duplicate_prediction_scenario_ids": duplicates,
        "model_quality_acceptance": "not_established_synthetic_contract_only",
        "scope_note": (
            "Deterministic exact-match scoring against synthetic decision contracts. "
            "Contract replay validates the scorer and pair distinctions; it is not model output, human adjudication, or model-quality acceptance."
        ),
        "pairs": pair_results,
    }
    return report


def write_template(gold_path: Path, output_path: Path) -> None:
    rows = read_rows(gold_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["scenario_id", *FIELDS])
        writer.writeheader()
        for row in rows:
            writer.writerow({"scenario_id": row["scenario_id"], **{field: "" for field in FIELDS}})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-dir", type=Path, default=DEFAULT_PACK)
    parser.add_argument("--adapted", type=Path)
    parser.add_argument("--runtime-report", type=Path)
    parser.add_argument("--predictions", type=Path, help="CSV with one predicted decision contract per scenario_id.")
    parser.add_argument("--replay-contracts", action="store_true", help="Score the gold contract against itself to check the deterministic scoring path.")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--write-template", type=Path, default=None)
    args = parser.parse_args()
    if args.replay_contracts == bool(args.predictions or args.adapted):
        parser.error("Provide exactly one of --predictions or --replay-contracts.")

    gold_path = args.pack_dir / "paired_investigation_scenarios.csv"
    gold_rows = read_rows(gold_path)
    if args.write_template:
        write_template(gold_path, args.write_template)
    if args.replay_contracts:
        prediction_rows = [
            {"scenario_id": row["scenario_id"], **{field: row[field] for field in FIELDS}}
            for row in gold_rows
        ]
        mode = "deterministic_contract_replay"
        default_output = args.pack_dir / "validation" / "paired_contract_replay_latest.json"
    elif args.adapted:
        from evaluation_bundle import validate_adapted, file_hash
        if not args.runtime_report or args.predictions:
            parser.error("Bound scoring requires --runtime-report and no CSV override")
        prediction_rows = validate_adapted(json.loads(args.runtime_report.read_text()), json.loads(args.adapted.read_text()), file_hash(args.runtime_report))
        mode = "bound_actual_business_contract"
        default_output = args.pack_dir / "validation" / "paired_investigation_score_latest.json"
    else:
        prediction_rows = read_rows(args.predictions)
        mode = "supplied_decision_predictions"
        default_output = args.pack_dir / "validation" / "paired_investigation_score_latest.json"
    report = score_rows(gold_rows, prediction_rows, mode=mode)
    report["business_schema_version"] = BUSINESS_SCHEMA_VERSION
    if args.adapted:
        from evaluation_bundle import file_hash, digest
        report["bindings"] = {"runtime_sha256": file_hash(args.runtime_report), "adapted_sha256": file_hash(args.adapted),
                              "predictions_sha256": digest(prediction_rows), "gold_path": str(gold_path.resolve()),
                              "gold_sha256": file_hash(gold_path), "scenario_ids": sorted(row["scenario_id"] for row in prediction_rows)}
    output_path = args.output or default_output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "scoring_mode": report["scoring_mode"],
        "pairs_passed": report["pairs_passed"],
        "pair_count": report["pair_count"],
        "model_quality_acceptance": report["model_quality_acceptance"],
        "output": str(output_path),
    }, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
