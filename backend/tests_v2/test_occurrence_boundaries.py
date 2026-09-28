from datetime import date
from decimal import Decimal

import pytest

from insight.metrics.cohort import calculate_cohort
from insight.metrics.facts import source_manifest
from insight.metrics.occurrence import calculate_occurrences
from tests_v2.test_metrics import report

pytestmark = pytest.mark.django_db


def refund(record, day, amount, currency="USD", chain="C1"):
    return {
        "record_id": record,
        "business_date": day,
        "order_line_id": "L1",
        "transaction_chain_id": chain,
        "amount": amount,
        "currency": currency,
        "seller_sku": "S1",
        "fulfillment": "FBA",
    }


def test_cross_month_occurrence_and_shipment_cohort_are_separate(metric_scope):
    _, shops = metric_scope
    start, end = date(2026, 6, 1), date(2026, 8, 4)
    report(
        metric_scope,
        "orders",
        [
            {
                "record_id": "L1",
                "order_id": "O1",
                "business_date": "2026-07-01",
                "shipped_date": "2026-07-01",
                "quantity": 4,
                "shipped_quantity": 4,
                "seller_sku": "S1",
                "fulfillment": "FBA",
                "status": "shipped",
            }
        ],
        start,
        end,
        "product_record",
        "shipped_date",
    )
    report(metric_scope, "refunds", [refund("R1", "2026-08-01", "3.50")], start, end, "product_record")
    manifest = source_manifest(shops[0])
    occurrence = calculate_occurrences(manifest, "B000TEST01", date(2026, 8, 5))["refunds"]
    cohort = calculate_cohort(manifest, "B000TEST01", date(2026, 8, 5), shops[0].timezone)["refunds"]
    assert occurrence["current"]["event_count"] == 1
    assert occurrence["current"]["amount_by_currency"] == {"USD": "3.5000"}
    assert cohort["current"]["denominator"] == 1 and cohort["current"]["numerator"] == 0
    assert cohort["late_order_count"] == 1
    assert "rate" not in occurrence["current"] and "units" not in occurrence["current"]


def test_partial_refunds_reversals_and_unknown_currency_never_sum_across_currencies(metric_scope):
    _, shops = metric_scope
    rows = [
        refund("R1", "2026-08-31", "10"),
        refund("R2", "2026-09-01", "-4"),
        refund("R3", "2026-09-01", "7", "EUR"),
        refund("R4", "2026-09-02", "2", "XXX"),
        refund("R5", "2026-09-02", "0"),
    ]
    source, _ = report(metric_scope, "refunds", rows, date(2026, 8, 1), date(2026, 9, 4), "product_record")
    before = source_manifest(shops[0])
    result = calculate_occurrences(before, "B000TEST01", date(2026, 9, 5))["refunds"]
    assert result["status"] == "ready"
    assert result["current"]["event_count"] == 3 and result["current"]["reversal_count"] == 1
    assert {k: Decimal(v) for k, v in result["current"]["amount_by_currency"].items()} == {
        "USD": 6,
        "EUR": 7,
        "XXX": 2,
    }
    assert len(result["current"]["raw_row_ids"]) == 5
    # Reimporting a transaction replaces its source version, not its event identity.
    report(
        metric_scope,
        "refunds",
        [refund("R1", "2026-08-31", "12")],
        date(2026, 8, 1),
        date(2026, 9, 4),
        "product_record",
        existing=source,
    )
    after = calculate_occurrences(source_manifest(shops[0]), "B000TEST01", date(2026, 9, 5))["refunds"]
    assert after["current"]["event_count"] == 3
    assert Decimal(after["current"]["amount_by_currency"]["USD"]) == 8
    assert (
        Decimal(
            calculate_occurrences(before, "B000TEST01", date(2026, 9, 5))["refunds"]["current"][
                "amount_by_currency"
            ]["USD"]
        )
        == 6
    )


@pytest.mark.parametrize("count,expected", [(4, False), (5, True)])
def test_zero_baseline_requires_five_actual_refund_transactions(metric_scope, count, expected):
    _, shops = metric_scope
    report(
        metric_scope,
        "refunds",
        [refund(f"R{i}", "2026-09-01", "1", chain=f"C{i}") for i in range(count)],
        date(2026, 8, 1),
        date(2026, 9, 4),
        "product_record",
    )
    result = calculate_occurrences(source_manifest(shops[0]), "B000TEST01", date(2026, 9, 5))
    assert result["refunds"]["baseline"]["event_count"] == 0
    assert bool(result["signals"]) is expected
    if expected:
        assert result["signals"][0]["metric"] == "occurrence_refunds"
        assert "质量" not in result["signals"][0]["title"]
