"""Compare two same-split semantic baseline runs without changing either run."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _types(case: dict[str, Any]) -> set[str]:
    value = case.get("error_type", [])
    return {value} if isinstance(value, str) else set(value)


def compare(*, v10_dir: Path, v11_dir: Path, output_dir: Path) -> dict[str, Any]:
    v10 = json.loads((v10_dir / "dev_eval_metrics.json").read_text(encoding="utf-8"))
    v11 = json.loads((v11_dir / "dev_eval_metrics.json").read_text(encoding="utf-8"))
    cases10 = {case["review_id"]: case for case in _jsonl(v10_dir / "bad_cases.jsonl")}
    cases11 = {case["review_id"]: case for case in _jsonl(v11_dir / "bad_cases.jsonl")}
    all_ids = sorted(set(cases10) | set(cases11))

    metrics = [
        "primary_topic_macro_f1", "secondary_topic_micro_f1", "topic_polarity_micro_f1",
        "matched_topic_polarity_accuracy", "evidence_substring_validity",
        "invalid_taxonomy_rate", "unsupported_evidence_rate",
    ]
    metric_comparison = {}
    for name in metrics:
        old = v10["metrics"][name]["value"]
        new = v11["metrics"][name]["value"]
        metric_comparison[name] = {
            "v1_0": old,
            "v1_1": new,
            "delta": new - old,
            "improved": new > old if name not in {"invalid_taxonomy_rate", "unsupported_evidence_rate"} else new < old,
            "v1_0_passed": v10["metrics"][name]["passed"],
            "v1_1_passed": v11["metrics"][name]["passed"],
        }

    error_names = ["missing_topic", "extra_topic", "polarity_mismatch", "guard_failure"]
    error_comparison = {}
    for name in error_names:
        old = sum(name in _types(case) for case in cases10.values())
        new = sum(name in _types(case) for case in cases11.values())
        error_comparison[name] = {"v1_0": old, "v1_1": new, "delta": new - old}
    error_comparison["bad_case_count"] = {"v1_0": len(cases10), "v1_1": len(cases11), "delta": len(cases11) - len(cases10)}

    statuses = []
    for rid in all_ids:
        old_types = _types(cases10.get(rid, {}))
        new_types = _types(cases11.get(rid, {}))
        if old_types and not new_types:
            status = "fixed"
        elif new_types and not old_types:
            status = "regressed"
        else:
            status = "unchanged"
        statuses.append({
            "review_id": rid,
            "status_vs_v1_0": status,
            "v1_0_error_type": sorted(old_types),
            "v1_1_error_type": sorted(new_types),
            "v1_1_case": cases11.get(rid),
        })
    # Keep a deterministic, stratified sample: status first, then most error categories, then id.
    statuses.sort(key=lambda row: (row["status_vs_v1_0"] != "regressed", row["status_vs_v1_0"] != "fixed", -len(row["v1_1_error_type"]), row["review_id"]))
    representatives = statuses[: max(20, min(60, len(statuses)))]

    result = {
        "comparison": {
            "v1_0_dir": str(v10_dir), "v1_1_dir": str(v11_dir),
            "same_split": v10.get("split") == v11.get("split") == "dev",
            "same_schema_and_taxonomy": v10.get("actual_provider_config", {}).get("taxonomy_version") == v11.get("actual_provider_config", {}).get("taxonomy_version"),
        },
        "metrics": metric_comparison,
        "errors": error_comparison,
        "status_counts": dict(Counter(row["status_vs_v1_0"] for row in statuses)),
        "representative_count": len(representatives),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "v1_0_v1_1_comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output_dir / "v1_0_v1_1_comparison.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["category", "metric_or_error", "v1_0", "v1_1", "delta", "improved"])
        for name, row in metric_comparison.items():
            writer.writerow(["metric", name, row["v1_0"], row["v1_1"], row["delta"], row["improved"]])
        for name, row in error_comparison.items():
            writer.writerow(["error", name, row["v1_0"], row["v1_1"], row["delta"], ""])
    with (output_dir / "prompt_v1_1_representatives.jsonl").open("w", encoding="utf-8") as handle:
        for row in representatives:
            case = row.get("v1_1_case") or {}
            output = {
                "review_id": row["review_id"],
                "status_vs_v1_0": row["status_vs_v1_0"],
                "v1_0_error_type": row["v1_0_error_type"],
                "v1_1_error_type": row["v1_1_error_type"],
                "review_text": case.get("text", ""),
                "golden_claims": case.get("golden_claims", []),
                "predicted_claims": case.get("predicted_claims", []),
            }
            handle.write(json.dumps(output, ensure_ascii=False) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v10-dir", type=Path, required=True)
    parser.add_argument("--v11-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(compare(v10_dir=args.v10_dir, v11_dir=args.v11_dir, output_dir=args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
