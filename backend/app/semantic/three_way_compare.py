"""Three-version Dev comparison with v1.2-focused case statuses."""

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


def compare(*, dirs: dict[str, Path], output_dir: Path) -> dict[str, Any]:
    metrics = {version: json.loads((path / "dev_eval_metrics.json").read_text(encoding="utf-8")) for version, path in dirs.items()}
    cases = {version: {case["review_id"]: case for case in _jsonl(path / "bad_cases.jsonl")} for version, path in dirs.items()}
    metric_names = ["primary_topic_macro_f1", "secondary_topic_micro_f1", "topic_polarity_micro_f1", "matched_topic_polarity_accuracy", "evidence_substring_validity", "invalid_taxonomy_rate", "unsupported_evidence_rate"]
    error_names = ["missing_topic", "extra_topic", "polarity_mismatch", "guard_failure"]
    metric_table = {name: {version: metrics[version]["metrics"][name]["value"] for version in dirs} for name in metric_names}
    error_table = {name: {version: sum(name in _types(case) for case in cases[version].values()) for version in dirs} for name in error_names}
    error_table["bad_case_count"] = {version: len(cases[version]) for version in dirs}
    all_ids = sorted(set().union(*(set(value) for value in cases.values())))
    statuses = []
    for rid in all_ids:
        old = _types(cases["v1.1"].get(rid, {}))
        new = _types(cases["v1.2"].get(rid, {}))
        status = "fixed" if old and not new else "regressed" if new and not old else "unchanged"
        case = cases["v1.2"].get(rid) or cases["v1.1"].get(rid) or cases["v1.0"].get(rid)
        predicted = case.get("predicted_claims", []) if case else []
        focus = []
        if "extra_topic" in new or any(str(c.get("secondary_topic")) == "general_performance" for c in predicted): focus.append("extra_or_general_performance")
        if "guard_failure" in new or "unsupported_evidence" in new: focus.append("evidence_or_guard")
        if "polarity_mismatch" in new: focus.append("polarity")
        if any(str(c.get("primary_topic")) not in {"function_performance","reliability_durability","compatibility_fit","safety_risk","usability_installation","convenience_value","product_quality","other_unknown"} for c in predicted): focus.append("cross_primary")
        statuses.append({"review_id": rid, "status_vs_v1_1": status, "v1_1_error_type": sorted(old), "v1_2_error_type": sorted(new), "focus": sorted(set(focus)), "review_text": case.get("text", "") if case else "", "golden_claims": case.get("golden_claims", []) if case else [], "predicted_claims": predicted})
    statuses.sort(key=lambda row: (not row["focus"], row["status_vs_v1_1"] != "regressed", -len(row["v1_2_error_type"]), row["review_id"]))
    representatives = statuses[: max(20, min(60, len(statuses)))]
    result = {"same_dev_split": all(metrics[v].get("split") == "dev" for v in dirs), "versions": {v: metrics[v]["actual_provider_config"] for v in dirs}, "metrics": metric_table, "errors": error_table, "status_counts_vs_v1_1": dict(Counter(row["status_vs_v1_1"] for row in statuses)), "representative_count": len(representatives)}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "v1_0_v1_1_v1_2_comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output_dir / "v1_0_v1_1_v1_2_comparison.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle); writer.writerow(["category", "name", "v1.0", "v1.1", "v1.2"])
        for name, row in metric_table.items(): writer.writerow(["metric", name, row["v1.0"], row["v1.1"], row["v1.2"]])
        for name, row in error_table.items(): writer.writerow(["error", name, row["v1.0"], row["v1.1"], row["v1.2"]])
    with (output_dir / "prompt_v1_2_focus_representatives.jsonl").open("w", encoding="utf-8") as handle:
        for row in representatives: handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--v10", type=Path, required=True); parser.add_argument("--v11", type=Path, required=True); parser.add_argument("--v12", type=Path, required=True); parser.add_argument("--output-dir", type=Path, required=True); args = parser.parse_args()
    print(json.dumps(compare(dirs={"v1.0": args.v10, "v1.1": args.v11, "v1.2": args.v12}, output_dir=args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
