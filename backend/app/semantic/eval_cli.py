"""Evaluate precomputed JSONL predictions against one Golden review split."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .evaluator import SemanticEvaluator
from .taxonomy import TaxonomyLoader


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _jsonl_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSONL at line {line_number}: {exc}") from exc
    return rows


def evaluate_files(
    *,
    review_path: Path,
    claim_path: Path,
    split_path: Path,
    prediction_path: Path,
    taxonomy_path: Path,
    split_name: str,
) -> dict:
    assignments = {row["review_id"]: row["split"] for row in _csv_rows(split_path)}
    selected_ids = {review_id for review_id, split in assignments.items() if split == split_name}
    if not selected_ids:
        raise ValueError(f"Unknown or empty split: {split_name}")

    reviews = {
        row["review_id"]: f"{row['title']}\n{row['text']}"
        for row in _csv_rows(review_path)
        if row["review_id"] in selected_ids
    }
    golden = [row for row in _csv_rows(claim_path) if row["review_id"] in selected_ids]
    predictions = _jsonl_rows(prediction_path)
    foreign_ids = sorted({str(row.get("review_id")) for row in predictions} - selected_ids)
    if foreign_ids:
        raise ValueError(f"Predictions contain review_ids outside {split_name}: {foreign_ids[:5]}")

    report = SemanticEvaluator(TaxonomyLoader.load(taxonomy_path)).evaluate(
        reviews=reviews,
        golden_claims=golden,
        predictions=predictions,
    )
    return {
        "split": split_name,
        "review_count": len(reviews),
        "golden_claim_count": len(golden),
        "prediction_count": len(predictions),
        "report": report.model_dump(mode="json"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--split-file", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--split", choices=("dev", "validation", "frozen_test"), required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate_files(
        review_path=args.reviews,
        claim_path=args.claims,
        split_path=args.split_file,
        prediction_path=args.predictions,
        taxonomy_path=args.taxonomy,
        split_name=args.split,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
