"""Prepare reproducible historical review inputs without exposing gold labels."""

import argparse
import csv
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTS = {"B07P9S6R9Y", "B0BYJ8ZVWH"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def normalized(text):
    return " ".join(text.casefold().split())


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = ROOT / "dataset/data/01_标准化评论数据/评论数据集_v1.csv"
    frozen = ROOT / "dataset/data/03_黄金评论集/黄金评论主表_v1_0.csv"
    reviews, gold = read(source), read(frozen)
    # Only identities and text are used for exclusion; no final/candidate labels.
    excluded_ids = {row["review_id"] for row in gold}
    excluded_text = {normalized(row["text"]) for row in gold}
    candidates = [row for row in reviews if row["sku"] in PRODUCTS
                  and row["review_id"] not in excluded_ids
                  and normalized(row["text"]) not in excluded_text]
    grouped = {}
    for row in sorted(candidates, key=lambda row: row["review_id"]):
        grouped.setdefault(normalized(row["text"]), row)
    pool = sorted(grouped.values(), key=lambda row: hashlib.sha256(
        ("real-demo-v1:" + normalized(row["text"])).encode()).hexdigest())
    selected = []
    for product in sorted(PRODUCTS):
        for low_rating in (False, True):
            bucket = [row for row in pool if row["sku"] == product
                      and (float(row["rating"]) <= 2) == low_rating]
            if len(bucket) < 75:
                raise ValueError("insufficient product/rating stratum")
            selected.extend(bucket[:75])
    output = args.output.resolve()
    if output.exists():
        raise ValueError("output must be new; existing evidence is never overwritten")
    output.mkdir(parents=True)
    # Runtime inputs have no rating-derived answers, labels, candidate topics or qrels.
    write_jsonl(output / "prediction-input.jsonl", [
        {"review_id": row["review_id"], "text": row["text"]} for row in selected])
    fields = ["review_id", "sku", "source_asin", "product_name", "rating", "title", "text", "created_at", "source"]
    write_jsonl(output / "review-provenance.jsonl", [
        {key: row[key] for key in fields} for row in selected])
    # Full original date series is retained for honest historical trend demonstrations.
    with (output / "historical-reviews.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row[key] for key in fields}
                         for row in reviews if row["sku"] in PRODUCTS)
    with (output / "independent-review.csv").open("w", encoding="utf-8", newline="") as stream:
        fields_review = ["review_id", "text", "topics", "polarity", "evidence_spans", "safety_judgment", "reviewer", "reviewed_at", "notes"]
        writer = csv.DictWriter(stream, fieldnames=fields_review)
        writer.writeheader()
        writer.writerows({"review_id": row["review_id"], "text": row["text"]} for row in selected)
    manifest = {"version": "real-review-demo-v1", "source_sha256": digest(source),
                "gold_exclusion_source_sha256": digest(frozen), "model_calls": 0,
                "quality_status": "not_run", "independent_review_status": "pending_user",
                "sample_rows": len(selected), "products": dict(Counter(row["sku"] for row in selected)),
                "sampling": "75 per product and low/non-low rating stratum; not prevalence representative",
                "historical_as_of": "2023-09-03T00:00:00Z", "timezone": "UTC",
                "gold_ids_and_normalized_text_excluded": True,
                "sample_normalized_text_unique": len({normalized(row["text"]) for row in selected}) == len(selected),
                "historical_series_contains_gold": True,
                "runtime_input": "prediction-input.jsonl",
                "runtime_forbidden": ["review-provenance.jsonl", "independent-review.csv", "historical-reviews.csv"],
                "files": {path.name: digest(path) for path in output.iterdir()}}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    runtime = output / "classification-candidate"
    (runtime / "snapshot/backend/app/semantic").mkdir(parents=True)
    (runtime / "snapshot/backend/app/__init__.py").write_text("", encoding="utf-8")
    for path in (ROOT / "backend/app/semantic").glob("*.py"):
        shutil.copyfile(path, runtime / "snapshot/backend/app/semantic" / path.name)
    shutil.copyfile(output / "prediction-input.jsonl", runtime / "prediction-input.jsonl")
    shutil.copyfile(ROOT / "dataset/data/03_黄金评论集/主题分类体系_v1_1.json", runtime / "taxonomy.json")
    shutil.copyfile(ROOT / "scripts/public_agent_prediction.py", runtime / "public_agent_prediction.py")
    references = [{"path": str(path.relative_to(runtime)), "sha256": digest(path)}
                  for path in sorted(runtime.rglob("*")) if path.is_file()]
    plan = {"repository": str(ROOT), "rows": 300, "model": "deepseek-flash",
            "status": "prepared_no_paid_dispatch", "runtime_references": references,
            "max_request_bytes": 8000, "max_output_tokens": 1200,
            "max_attempts": 1, "stop_on_first_failure": True,
            "requested_total_hard_cap_usd": "1.20", "requested_call_hard_cap_usd": "0.004",
            "scope": "real charger review classification; user independent judgments pending",
            "source_manifest_sha256": digest(output / "manifest.json"),
            "independent_labels_in_runtime": False, "broad_uci_mapping_used": False}
    (runtime / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (runtime / "approval.json").write_text(json.dumps({"approved": False,
        "reason": "new real-review candidate; previous stopped round is preserved"}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "rows": len(selected), "model_calls": 0}))


if __name__ == "__main__":
    main()
