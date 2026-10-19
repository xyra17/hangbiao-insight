"""Deterministic review-level Golden split generator."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

SPLIT_VERSION = "golden-semantic-split-v1.0"
SPLIT_SEED = 20260912
SPLIT_RATIOS = {"dev": 0.6, "validation": 0.2, "frozen_test": 0.2}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_split(reviews: list[dict[str, str]], claims: list[dict[str, str]]) -> list[dict[str, str]]:
    """Greedily balances product and common-topic coverage, never individual claims."""
    review_by_id = {row["review_id"]: row for row in reviews}
    if len(review_by_id) != len(reviews):
        raise ValueError("review_id must be unique in the Golden review table")
    topics_by_review: dict[str, set[str]] = defaultdict(set)
    for claim in claims:
        rid = claim["review_id"]
        if rid not in review_by_id:
            raise ValueError(f"Claim references unknown review_id: {rid}")
        topics_by_review[rid].add(claim["secondary_topic"])

    total = len(reviews)
    targets = {"dev": round(total * 0.6), "validation": round(total * 0.2)}
    targets["frozen_test"] = total - targets["dev"] - targets["validation"]
    product_totals = Counter(row["product_id"] for row in reviews)
    topic_totals = Counter(topic for topics in topics_by_review.values() for topic in topics)
    assigned_product: dict[str, Counter] = defaultdict(Counter)
    assigned_topic: dict[str, Counter] = defaultdict(Counter)
    counts = Counter()

    def stable_hash(rid: str) -> str:
        return hashlib.sha256(f"{SPLIT_SEED}:{rid}".encode()).hexdigest()

    ordered = sorted(
        reviews,
        key=lambda row: (
            min((topic_totals[t] for t in topics_by_review[row["review_id"]]), default=10**9),
            product_totals[row["product_id"]],
            stable_hash(row["review_id"]),
        ),
    )
    assignments: dict[str, str] = {}
    for row in ordered:
        rid, product = row["review_id"], row["product_id"]
        candidates = [name for name in SPLIT_RATIOS if counts[name] < targets[name]]

        def score(name: str) -> tuple[float, float, str]:
            product_target = product_totals[product] * SPLIT_RATIOS[name]
            product_deficit = (product_target - assigned_product[name][product]) / max(product_target, 1)
            topic_deficit = sum(
                (topic_totals[t] * SPLIT_RATIOS[name] - assigned_topic[name][t])
                / max(topic_totals[t] * SPLIT_RATIOS[name], 1)
                for t in topics_by_review[rid]
            )
            capacity = (targets[name] - counts[name]) / targets[name]
            tie_break = hashlib.sha256(f"{SPLIT_SEED}:{rid}:{name}".encode()).hexdigest()
            return (topic_deficit + product_deficit + capacity * 0.05, capacity, tie_break)

        chosen = max(candidates, key=score)
        assignments[rid] = chosen
        counts[chosen] += 1
        assigned_product[chosen][product] += 1
        assigned_topic[chosen].update(topics_by_review[rid])

    return [
        {
            "review_id": row["review_id"],
            "product_id": row["product_id"],
            "split": assignments[row["review_id"]],
            "split_version": SPLIT_VERSION,
            "seed": str(SPLIT_SEED),
        }
        for row in sorted(reviews, key=lambda item: item["review_id"])
    ]


def write_artifacts(review_path: Path, claim_path: Path, output_csv: Path, metadata_path: Path) -> None:
    claim_rows = _read_csv(claim_path)
    assignments = build_split(_read_csv(review_path), claim_rows)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(assignments[0]))
        writer.writeheader()
        writer.writerows(assignments)
    counts = Counter(row["split"] for row in assignments)
    split_by_review = {row["review_id"]: row["split"] for row in assignments}
    topic_support: dict[str, Counter[str]] = defaultdict(Counter)
    for claim in claim_rows:
        topic_support[claim["secondary_topic"]][split_by_review[claim["review_id"]]] += 1
    metadata = {
        "split_version": SPLIT_VERSION,
        "seed": SPLIT_SEED,
        "unit": "review_id",
        "ratios": SPLIT_RATIOS,
        "counts": dict(counts),
        "secondary_topic_support": {
            topic: {split: support.get(split, 0) for split in SPLIT_RATIOS}
            for topic, support in sorted(topic_support.items())
        },
        "frozen_test_policy": "Never expose frozen_test labels to prompts or prompt tuning.",
        "review_source": review_path.name,
        "claim_source": claim_path.name,
        "review_source_sha256": _sha256(review_path),
        "claim_source_sha256": _sha256(claim_path),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()
    write_artifacts(args.reviews, args.claims, args.output, args.metadata)


if __name__ == "__main__":
    main()
