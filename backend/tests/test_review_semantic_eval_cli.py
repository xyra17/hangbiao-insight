import csv
import json
from pathlib import Path

import pytest

from app.semantic.eval_cli import evaluate_files


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_eval_files_filters_labels_by_review_split_and_rejects_foreign_predictions(tmp_path):
    reviews = tmp_path / "reviews.csv"
    claims = tmp_path / "claims.csv"
    split = tmp_path / "split.csv"
    predictions = tmp_path / "predictions.jsonl"
    taxonomy = Path(__file__).resolve().parents[2] / "dataset/data/03_黄金评论集/主题分类体系_v1_1.json"
    write_csv(reviews, [
        {"review_id": "r1", "title": "Fast", "text": "Works well."},
        {"review_id": "r2", "title": "Slow", "text": "Not good."},
    ])
    write_csv(claims, [
        {"review_id": "r1", "primary_topic": "function_performance", "secondary_topic": "charging_speed",
         "polarity": "positive", "evidence_span": "Fast"},
        {"review_id": "r2", "primary_topic": "function_performance", "secondary_topic": "charging_speed",
         "polarity": "negative", "evidence_span": "Slow"},
    ])
    write_csv(split, [{"review_id": "r1", "split": "dev"}, {"review_id": "r2", "split": "frozen_test"}])
    predictions.write_text(json.dumps({
        "review_id": "r1", "primary_topic": "function_performance", "secondary_topic": "charging_speed",
        "polarity": "positive", "evidence_span": "Fast",
    }) + "\n", encoding="utf-8")
    result = evaluate_files(
        review_path=reviews, claim_path=claims, split_path=split, prediction_path=predictions,
        taxonomy_path=taxonomy, split_name="dev",
    )
    assert result["review_count"] == 1
    assert result["golden_claim_count"] == 1
    assert result["report"]["secondary_topic_micro_f1"]["value"] == 1

    predictions.write_text(json.dumps({
        "review_id": "r2", "primary_topic": "function_performance", "secondary_topic": "charging_speed",
        "polarity": "negative", "evidence_span": "Slow",
    }) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside dev"):
        evaluate_files(
            review_path=reviews, claim_path=claims, split_path=split, prediction_path=predictions,
            taxonomy_path=taxonomy, split_name="dev",
        )
