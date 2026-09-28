"""Reproducible, read-only aggregation of a Dev semantic baseline."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _claim_tuple(claim: dict[str, Any]) -> tuple[str, str, str]:
    return (str(claim.get("secondary_topic", "")), str(claim.get("polarity", "")), str(claim.get("evidence_span", "")))


def _error_types(case: dict[str, Any]) -> list[str]:
    raw = case.get("error_type", [])
    return [raw] if isinstance(raw, str) else list(raw)


def analyze(*, metrics_path: Path, bad_cases_path: Path, parsed_dir: Path, reviews_path: Path, claims_path: Path, output_dir: Path) -> dict[str, Any]:
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    cases = _jsonl(bad_cases_path)
    reviews = {row["review_id"]: row for row in _csv(reviews_path)}
    golden_by_review: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in _csv(claims_path):
        # The source claims table is already filtered to Dev by review_id in this baseline report.
        if row["review_id"] in {case["review_id"] for case in cases}:
            golden_by_review[row["review_id"]].append(row)

    # Prefer parsed artifacts as the prediction source; this verifies the report is based on saved results.
    parsed_by_review: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(parsed_dir.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        parsed_by_review[path.stem] = value.get("predictions", [])

    error_counts = Counter(error for case in cases for error in _error_types(case))
    total_cases = len(cases)
    error_distribution = {
        error: {"count": count, "ratio": count / total_cases if total_cases else 0.0}
        for error, count in sorted(error_counts.items())
    }

    missed = Counter()
    extra = Counter()
    confusion = Counter()
    invalid_parent = Counter()
    invalid_secondary = Counter()
    invalid_pairs = Counter()
    evidence_errors = Counter()
    matched_polarity_errors = 0
    unmatched_polarity = 0
    representative_rows: list[dict[str, Any]] = []

    for case in cases:
        rid = case["review_id"]
        golden = golden_by_review[rid]
        predicted = parsed_by_review.get(rid, case.get("predicted_claims", []))
        gold_topics = Counter(row["secondary_topic"] for row in golden)
        pred_topics = Counter(str(row.get("secondary_topic", "")) for row in predicted)
        for topic, count in (gold_topics - pred_topics).items():
            missed[topic] += count
        for topic, count in (pred_topics - gold_topics).items():
            extra[topic] += count
        gold_pairs = Counter((row["secondary_topic"], row["polarity"]) for row in golden)
        pred_pairs = Counter((str(row.get("secondary_topic", "")), str(row.get("polarity", ""))) for row in predicted)
        for (gold_topic, _), count in (gold_pairs - pred_pairs).items():
            if gold_topic in pred_topics:
                matched_polarity_errors += count
            else:
                unmatched_polarity += count
        for (topic, polarity), count in (pred_pairs - gold_pairs).items():
            if topic in gold_topics:
                matched_polarity_errors += count
        for gold_topic in gold_topics:
            for pred_topic in pred_topics:
                if gold_topic != pred_topic:
                    # Count review-level topic confusion, preserving direction.
                    confusion[(gold_topic, pred_topic)] += min(gold_topics[gold_topic], pred_topics[pred_topic])

        for prediction in predicted:
            primary = str(prediction.get("primary_topic", ""))
            secondary = str(prediction.get("secondary_topic", ""))
            evidence = str(prediction.get("evidence_span", "") or "")
            if primary not in {row["primary_topic"] for row in golden}:
                invalid_parent[primary] += 1
            valid_pairs = {(row["primary_topic"], row["secondary_topic"]) for row in golden}
            if secondary not in {row["secondary_topic"] for row in golden}:
                invalid_secondary[secondary] += 1
            if (primary, secondary) not in valid_pairs and primary and secondary:
                invalid_pairs[(primary, secondary)] += 1
            source = str(case.get("text", reviews.get(rid, {}).get("text", "")))
            if not evidence:
                evidence_errors["missing"] += 1
            elif evidence not in source:
                evidence_errors["not_continuous_substring"] += 1
            elif len(evidence) > 120 or len(evidence) > max(1, int(len(source) * 0.6)):
                evidence_errors["too_long"] += 1
            if primary not in {row["primary_topic"] for row in golden} or secondary not in {row["secondary_topic"] for row in golden}:
                evidence_errors["topic_unsupported"] += 1

        note_parts = []
        types = _error_types(case)
        if "missing_topic" in types:
            note_parts.append("模型漏掉了 Golden 中至少一个 Topic")
        if "extra_topic" in types:
            note_parts.append("模型增加了 Golden 未标注的 Topic")
        if "polarity_mismatch" in types:
            note_parts.append("Topic 已匹配但 Polarity 不一致")
        if "guard_failure" in types:
            note_parts.append("输出触发 Taxonomy 或 Evidence Guard")
        representative_rows.append({
            "review_id": rid,
            "review_text": case.get("text", reviews.get(rid, {}).get("text", "")),
            "golden_claims": golden,
            "predicted_claims": predicted,
            "error_type": types,
            "analysis_note": "；".join(note_parts) or "综合错误，需人工复核",
        })

    representative_rows.sort(key=lambda row: (len(row["error_type"]), row["review_id"]), reverse=True)
    representatives = representative_rows[: max(30, min(60, len(representative_rows)))]
    result = {
        "source": {
            "baseline_metrics": str(metrics_path),
            "bad_cases": str(bad_cases_path),
            "parsed_dir": str(parsed_dir),
            "prompt_version": metrics.get("actual_provider_config", {}).get("prompt_version"),
            "taxonomy_version": metrics.get("actual_provider_config", {}).get("taxonomy_version"),
            "read_only": True,
        },
        "baseline": {
            "review_count": metrics.get("review_count"),
            "golden_claim_count": metrics.get("golden_claim_count"),
            "prediction_count": metrics.get("prediction_count"),
            "bad_case_count": total_cases,
        },
        "error_type_distribution": error_distribution,
        "topic_missed": [{"golden_topic": topic, "count": count, "ratio": count / max(1, sum(missed.values()))} for topic, count in missed.most_common()],
        "topic_extra": [{"predicted_topic": topic, "count": count} for topic, count in extra.most_common()],
        "topic_confusion": [{"golden_topic": gold, "predicted_topic": pred, "count": count} for (gold, pred), count in confusion.most_common()],
        "invalid_taxonomy": {
            "invalid_parent_topic": dict(invalid_parent.most_common()),
            "invalid_secondary_topic": dict(invalid_secondary.most_common()),
            "parent_secondary_mismatch": [{"parent_topic": parent, "secondary_topic": secondary, "count": count} for (parent, secondary), count in invalid_pairs.most_common()],
        },
        "evidence_errors": dict(evidence_errors.most_common()),
        "polarity_errors": {
            "matched_topic_polarity_error_count": matched_polarity_errors,
            "unmatched_topic_polarity_undetermined_count": unmatched_polarity,
        },
        "representative_count": len(representatives),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "bad_case_error_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output_dir / "bad_case_error_summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["category", "key", "value", "ratio"])
        for error, value in error_distribution.items():
            writer.writerow(["error_type", error, value["count"], value["ratio"]])
        for row in result["topic_missed"]:
            writer.writerow(["topic_missed", row["golden_topic"], row["count"], row["ratio"]])
        for row in result["topic_extra"]:
            writer.writerow(["topic_extra", row["predicted_topic"], row["count"], ""])
        for row in result["topic_confusion"]:
            writer.writerow(["topic_confusion", f"{row['golden_topic']} -> {row['predicted_topic']}", row["count"], ""])
        for key, value in result["evidence_errors"].items():
            writer.writerow(["evidence_error", key, value, ""])
    with (output_dir / "bad_case_representatives.jsonl").open("w", encoding="utf-8") as handle:
        for row in representatives:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--bad-cases", type=Path, required=True)
    parser.add_argument("--parsed-dir", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(analyze(metrics_path=args.metrics, bad_cases_path=args.bad_cases, parsed_dir=args.parsed_dir, reviews_path=args.reviews, claims_path=args.claims, output_dir=args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
