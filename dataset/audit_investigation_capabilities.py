"""Separate execution, evidence and business-action differences after runtime.

Pair membership comes from an already generated scoring report, never used to
plan tools or produce business outputs. No expected decision fields are read.
"""
import argparse
import json
from pathlib import Path


def audit(runtime):
    traces = {row["scenario_id"]: row for row in runtime["trajectories"]}
    comparisons = []
    for pair in runtime.get("scoring", {}).get("pairs", []):
        rows = [traces[variant["scenario_id"]] for variant in pair["variants"]]
        steps = [row.get("result", {}).get("business_contract", {}).get("expected_next_step", "unknown") for row in rows]
        records = [{item["record_id"] for search in row.get("retrievals", []) for item in search.get("items", []) if item.get("match_kind") == "relevant_hit"} for row in rows]
        # Capture exports record_ids separately; retain that exact scoped evidence.
        records = [record | {key for search in row.get("retrievals", []) for key in search.get("record_ids", [])} for row, record in zip(rows, records, strict=True)]
        comparisons.append({"pair_id": pair["pair_id"], "next_steps": steps,
                            "different_scoped_evidence": len({frozenset(keys) for keys in records}) > 1,
                            "different_known_next_step": "unknown" not in steps and len(set(steps)) > 1,
                            "semantic_correctness": "not_established_without_independent_adjudication"})
    return {"schema_version": "investigation-capability-audit-v1", "terminal_success": runtime["run_terminal_success_count"],
            "scenario_count": len(traces), "pair_count": len(comparisons),
            "different_scoped_evidence_pairs": sum(row["different_scoped_evidence"] for row in comparisons),
            "different_known_next_step_pairs": sum(row["different_known_next_step"] for row in comparisons),
            "tool_or_parameter_difference_pairs": runtime["pairs_with_tool_or_parameter_divergence"],
            "evidence_driven_parameter_difference_pairs": runtime["pairs_with_evidence_driven_follow_up_parameter_divergence"],
            "semantic_correctness": "not_established", "pairs": comparisons,
            "limitation": "Next-step rules use completeness and scoped hit existence, not meaning/causality; a changed next step is not a correct conclusion."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit(json.loads(args.runtime_report.read_text())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
