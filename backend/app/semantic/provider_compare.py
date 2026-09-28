"""Compare the frozen-prompt DeepSeek fallback with the three preserved Qwen Dev runs."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


METRICS = (
    "primary_topic_macro_f1",
    "secondary_topic_micro_f1",
    "topic_polarity_micro_f1",
    "matched_topic_polarity_accuracy",
    "evidence_substring_validity",
    "invalid_taxonomy_rate",
    "unsupported_evidence_rate",
)
ERROR_TYPES = ("missing_topic", "extra_topic", "polarity_mismatch", "guard_failure")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _error_types(case: dict[str, Any]) -> set[str]:
    value = case.get("error_type", [])
    return {value} if isinstance(value, str) else set(value)


def _usage(run_dir: Path) -> dict[str, int]:
    totals: Counter[str] = Counter()
    for path in (run_dir / "raw").glob("*.json"):
        usage = (_read_json(path).get("raw_response") or {}).get("usage") or {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                totals[key] += value
        output_details = usage.get("output_tokens_details") or {}
        if isinstance(output_details.get("reasoning_tokens"), int):
            totals["reasoning_tokens"] += output_details["reasoning_tokens"]
        input_details = usage.get("input_tokens_details") or {}
        if isinstance(input_details.get("cached_tokens"), int):
            totals["cached_tokens"] += input_details["cached_tokens"]
    totals["response_count_with_raw_file"] = sum(1 for _ in (run_dir / "raw").glob("*.json"))
    return dict(totals)


def compare(*, runs: dict[str, Path], output_dir: Path) -> dict[str, Any]:
    reports = {name: _read_json(path / "dev_eval_metrics.json") for name, path in runs.items()}
    cases = {name: _read_jsonl(path / "bad_cases.jsonl") for name, path in runs.items()}
    metric_table = {
        metric: {name: reports[name]["metrics"][metric]["value"] for name in runs}
        for metric in METRICS
    }
    error_table = {
        error: {
            name: sum(error in _error_types(case) for case in cases[name]) for name in runs
        }
        for error in ERROR_TYPES
    }
    error_table["evidence_invalid_claims"] = {
        name: (
            reports[name]["metrics"]["evidence_substring_validity"]["denominator"]
            - reports[name]["metrics"]["evidence_substring_validity"]["numerator"]
        )
        for name in runs
    }
    error_table["unsupported_evidence_claims"] = {
        name: reports[name]["metrics"]["unsupported_evidence_rate"]["numerator"]
        for name in runs
    }
    error_table["matched_topic_polarity_incorrect_claims"] = {
        name: (
            reports[name]["metrics"]["matched_topic_polarity_accuracy"]["denominator"]
            - reports[name]["metrics"]["matched_topic_polarity_accuracy"]["numerator"]
        )
        for name in runs
    }
    error_table["bad_case_reviews"] = {name: len(cases[name]) for name in runs}

    deepseek = "deepseek-v4-flash / prompt-v1.0"
    qwen = "qwen3.5-9b / prompt-v1.0"
    delta_vs_qwen_v10 = {
        metric: metric_table[metric][deepseek] - metric_table[metric][qwen]
        for metric in METRICS
    }
    result = {
        "scope": {
            "split": "dev",
            "review_count": 216,
            "prompt_for_provider_comparison": "semantic-prompt-v1.0",
            "taxonomy_version": "taxonomy-v1.1",
            "validation_or_test_executed": False,
            "business_reviews_executed": False,
        },
        "versions": {name: reports[name]["actual_provider_config"] for name in runs},
        "metrics": metric_table,
        "errors": error_table,
        "error_units": {
            "missing_topic": "reviews",
            "extra_topic": "reviews",
            "polarity_mismatch": "reviews",
            "guard_failure": "reviews",
            "evidence_invalid_claims": "claims",
            "unsupported_evidence_claims": "claims",
            "matched_topic_polarity_incorrect_claims": "matched topics",
            "bad_case_reviews": "reviews",
        },
        "deepseek_delta_vs_qwen_prompt_v1_0": delta_vs_qwen_v10,
        "deepseek_token_usage": _usage(runs[deepseek]),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "deepseek_qwen_dev_comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (output_dir / "deepseek_qwen_dev_comparison.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["category", "name", *runs])
        for metric, values in metric_table.items():
            writer.writerow(["metric", metric, *(values[name] for name in runs)])
        for error, values in error_table.items():
            writer.writerow(["error", error, *(values[name] for name in runs)])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qwen-v10", type=Path, required=True)
    parser.add_argument("--qwen-v11", type=Path, required=True)
    parser.add_argument("--qwen-v12", type=Path, required=True)
    parser.add_argument("--deepseek", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    runs = {
        "qwen3.5-9b / prompt-v1.0": args.qwen_v10,
        "qwen3.5-9b / prompt-v1.1": args.qwen_v11,
        "qwen3.5-9b / prompt-v1.2": args.qwen_v12,
        "deepseek-v4-flash / prompt-v1.0": args.deepseek,
    }
    print(json.dumps(compare(runs=runs, output_dir=args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
