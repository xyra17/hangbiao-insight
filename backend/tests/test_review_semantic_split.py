import csv
from collections import Counter
from pathlib import Path

from app.semantic.split import SPLIT_SEED, SPLIT_VERSION, build_split


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "dataset/data/03_黄金评论集"


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_real_golden_split_is_deterministic_grouped_and_exact_size():
    reviews = read_csv(DATA_DIR / "黄金评论主表_v1_0.csv")
    claims = read_csv(DATA_DIR / "黄金评论标签表_v1_0.csv")
    first = build_split(reviews, claims)
    second = build_split(list(reversed(reviews)), list(reversed(claims)))

    assert first == second
    assert len(first) == 360
    assert len({row["review_id"] for row in first}) == 360
    assert Counter(row["split"] for row in first) == {
        "dev": 216, "validation": 72, "frozen_test": 72
    }
    assert {row["split_version"] for row in first} == {SPLIT_VERSION}
    assert {row["seed"] for row in first} == {str(SPLIT_SEED)}


def test_split_rejects_claim_leakage_from_unknown_review():
    reviews = [{"review_id": "r1", "product_id": "p1"}]
    claims = [{"review_id": "r2", "secondary_topic": "charging_speed"}]
    try:
        build_split(reviews, claims)
    except ValueError as exc:
        assert "unknown review_id" in str(exc)
    else:
        raise AssertionError("unknown review should have been rejected")
