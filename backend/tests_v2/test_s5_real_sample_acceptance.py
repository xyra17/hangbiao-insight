"""Acceptance guard for the frozen, desensitized golden review sample.

The files are a frozen evaluation corpus, not a live Amazon feed.  Keeping this
test separate from synthetic metric fixtures prevents a successful unit test
from being reported as real account validation.
"""

import csv
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "dataset" / "data" / "03_黄金评论集"


def read_csv(name):
    with (GOLDEN_DIR / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_desensitized_golden_reviews_are_frozen_and_joinable():
    reviews = read_csv("黄金评论主表_v1_0.csv")
    claims = read_csv("黄金评论标签表_v1_0.csv")
    assert len(reviews) >= 300
    assert len(claims) >= 600
    assert {row["review_status"] for row in reviews} == {"golden_frozen"}
    assert {row["golden_status"] for row in claims} == {"frozen"}
    assert all(row["text"].strip() for row in reviews)
    texts = {row["review_id"]: f"{row['title']} {row['text']}" for row in reviews}
    assert all(row["evidence_span"].strip().casefold() in texts[row["review_id"]].casefold() for row in claims)
    assert {row["product_id"] for row in reviews} >= {"B07H2V5YLH", "B07P9S6R9Y", "B0BYJ8ZVWH"}


def test_desensitized_golden_reviews_do_not_contain_obvious_personal_fields():
    reviews = read_csv("黄金评论主表_v1_0.csv")
    forbidden = re.compile(r"(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\+?\d[\d\s-]{7,}|邮编|身份证)", re.I)
    assert not any(forbidden.search(row["text"]) for row in reviews)
