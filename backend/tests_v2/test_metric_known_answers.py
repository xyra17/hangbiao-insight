from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from insight.metrics.cohort import cohort_from_facts
from insight.metrics.statistics import newcombe, wilson


def order(line, *, order_id="order-1", shipped=date(2026, 6, 10), purchased=date(2026, 6, 10)):
    return SimpleNamespace(
        record_id=line,
        raw_row_id=line,
        order_id=order_id,
        business_date=purchased,
        shipped_date=shipped,
        shipped_at=None,
        shipped_quantity=1,
    )


def refund(record, line, day, amount, chain="chain-1", currency="USD"):
    return SimpleNamespace(
        record_id=record,
        raw_row_id=record,
        order_line_id=line,
        business_date=day,
        occurred_at=None,
        amount=Decimal(amount),
        transaction_chain_id=chain,
        currency=currency,
    )


def coverage():
    start = date(2026, 4, 1)
    return {start + timedelta(days=i): "ready" for i in range(122)}


def calculate(orders, refunds=(), returns=(), order_days=None, event_days=None):
    return cohort_from_facts(
        coverage() if order_days is None else order_days,
        {"refunds": coverage() if event_days is None else event_days, "returns": coverage()},
        orders,
        refunds,
        returns,
        date(2026, 8, 1),
        "America/Los_Angeles",
    )


def test_split_shipment_uses_object_first_shipment_and_deduplicates_order():
    orders = [order("line-1"), order("line-2", shipped=date(2026, 6, 25))]
    result = calculate(orders, [refund("r1", "line-2", date(2026, 7, 15), "5")])["refunds"]
    assert result["current"]["denominator"] == 1
    assert result["current"]["numerator"] == 0
    assert result["late_order_count"] == 1
    result = calculate(
        orders,
        [refund("r1", "line-1", date(2026, 6, 12), "2"), refund("r2", "line-2", date(2026, 6, 28), "3")],
    )["refunds"]
    assert result["current"]["denominator"] == result["current"]["numerator"] == 1


def test_late_reversal_revises_validity_without_erasing_positive_evidence():
    positive = refund("r1", "line-1", date(2026, 6, 12), "10")
    partial = refund("r2", "line-1", date(2026, 7, 25), "-4")
    final = refund("r3", "line-1", date(2026, 7, 30), "-6")
    assert calculate([order("line-1")], [positive, partial])["refunds"]["current"]["numerator"] == 1
    result = calculate([order("line-1")], [positive, partial, final])["refunds"]
    assert result["current"]["numerator"] == 0
    # Fully reversed chains remain inspectable in the calculation's evidence.
    assert set(result["raw_row_ids"]) == {"r1", "r2", "r3"}


def test_first_shipment_requires_coverage_since_purchase():
    days = coverage()
    del days[date(2026, 5, 1)]
    result = calculate([order("line-1", purchased=date(2026, 4, 20))], order_days=days)["refunds"]
    assert result["status"] == "partial"
    assert result["current"]["unverified_first_shipment_orders"] == 1
    assert result["current"]["denominator"] == 0
    assert result["current"]["rate"] is None


def test_observation_coverage_gap_cannot_become_healthy_zero_rate():
    days = coverage()
    del days[date(2026, 6, 20)]
    result = calculate([order("line-1")], event_days=days)
    assert result["refunds"]["status"] == "partial"
    assert result["refunds"]["current"]["immature_orders"] == 1
    assert result["refunds"]["current"]["rate"] is None
    assert result["signals"] == []


def test_refund_without_return_and_unlinked_refund_are_distinct():
    refunds = [refund("r1", "line-1", date(2026, 6, 12), "1")]
    result = calculate([order("line-1")], refunds)
    assert result["refunds"]["current"]["numerator"] == 1
    assert result["returns"]["current"]["numerator"] == 0
    result = calculate([order("line-1")], refunds + [refund("r2", "unknown", date(2026, 6, 13), "9")])
    assert result["refunds"]["status"] == "partial"
    assert result["refunds"]["unmatched_records"] == 1
    assert result["signals"] == []


def test_different_currencies_and_chains_never_cancel_each_other():
    refunds = [
        refund("r1", "line-1", date(2026, 6, 12), "10", currency="USD"),
        refund("r2", "line-1", date(2026, 6, 13), "-10", currency="EUR"),
        refund("r3", "line-1", date(2026, 6, 14), "-10", chain="other", currency="USD"),
    ]
    assert calculate([order("line-1")], refunds)["refunds"]["current"]["numerator"] == 1


@pytest.mark.parametrize(
    "successes,total", [(1, 0), (-1, 0), (0, -1), (2, 1), (0.5, 1), (1, 1.5), (True, 1), (0, float("nan"))]
)
def test_invalid_counts_are_rejected_even_with_empty_other_window(successes, total):
    with pytest.raises(ValueError):
        wilson(successes, total)
    with pytest.raises(ValueError):
        newcombe(0, 0, successes, total)


@pytest.mark.parametrize("n", [1, 100, 10_000_000])
def test_wilson_extremes_match_closed_form_and_complements(n):
    z_squared = Decimal("1.959963984540054") ** 2
    bound = float(z_squared / (Decimal(n) + z_squared))
    assert wilson(0, n) == pytest.approx([0, bound], abs=1e-14)
    assert wilson(n, n) == pytest.approx([1 - bound, 1], abs=1e-14)
    lo, hi = newcombe(n, n, 0, n)
    assert lo > -1 and hi == pytest.approx(1)
