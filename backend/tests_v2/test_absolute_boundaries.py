from datetime import date
from decimal import Decimal

import pytest

from insight.metrics.absolute import calculate_absolute
from insight.metrics.facts import source_manifest
from tests_v2.test_metrics import report

pytestmark = pytest.mark.django_db


def test_page_rating_and_collected_review_average_have_independent_denominators(metric_scope):
    _, shops = metric_scope
    today = date(2026, 9, 5)
    report(
        metric_scope,
        "ratings",
        [{"business_date": "2026-09-04", "rating": "4.60", "rating_count": 1000}],
        date(2026, 9, 4),
        date(2026, 9, 4),
    )
    reviews = [
        {
            "business_date": day,
            "record_id": rid,
            "text": "Sample review",
            "language": "en",
            "rating": rating,
            "collected_at": "2026-09-05T00:00:00Z",
            "is_deleted": deleted,
        }
        for rid, day, rating, deleted in [
            ("old", "2026-08-07", 5, False),
            ("first", "2026-08-08", 1, False),
            ("last", "2026-09-04", 3, False),
            ("deleted", "2026-09-04", 5, True),
            ("incomplete-day", "2026-09-05", 5, False),
        ]
    ]
    report(metric_scope, "reviews", reviews, date(2026, 8, 7), today, "asin_record")
    result = calculate_absolute(source_manifest(shops[0]), "B000TEST01", today)
    assert Decimal(result["ratings"]["rating"]) == Decimal("4.60")
    sample = result["sampled_reviews"]
    assert sample["status"] == "sample_only" and sample["count"] == 2
    assert Decimal(sample["mean_rating"]) == Decimal(2)
    assert len(sample["raw_row_ids"]) == 2
    assert result["signals"] == []  # the low sample mean cannot trigger page-star decline


@pytest.mark.parametrize(
    "interval,drop,expected", [(7, "4.30", True), (8, "4.30", False), (7, "4.31", False)]
)
def test_page_rating_comparison_boundary(metric_scope, interval, drop, expected):
    from datetime import timedelta

    _, shops = metric_scope
    end = date(2026, 9, 4)
    start = end - timedelta(days=interval)
    report(
        metric_scope,
        "ratings",
        [{"business_date": start, "rating": "4.50"}, {"business_date": end, "rating": drop}],
        start,
        end,
    )
    result = calculate_absolute(source_manifest(shops[0]), "B000TEST01", date(2026, 9, 5))
    assert bool(result["signals"]) is expected
    assert result["sampled_reviews"]["mean_rating"] is None
    assert result["sampled_reviews"]["status"] == "missing"


def test_first_inventory_snapshot_unknown_and_unavailable_are_distinct(metric_scope):
    _, shops = metric_scope
    start = date(2026, 9, 4)
    source, _ = report(
        metric_scope,
        "inventory",
        [{"business_date": start, "available": "", "total": 12, "listing_status": "active"}],
        start,
        start,
    )
    result = calculate_absolute(source_manifest(shops[0]), "B000TEST01", date(2026, 9, 5))
    assert result["inventory"]["available"] is None and not result["signals"]
    report(
        metric_scope,
        "inventory",
        [{"business_date": start, "available": 0, "total": 12, "listing_status": "suppressed"}],
        start,
        start,
        existing=source,
    )
    result = calculate_absolute(source_manifest(shops[0]), "B000TEST01", date(2026, 9, 5))
    assert result["signals"][0]["metric"] == "availability"
    assert result["signals"][0]["priority"] == 0
