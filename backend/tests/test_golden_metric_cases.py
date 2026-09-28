import csv
from calendar import monthrange
from datetime import date
from pathlib import Path

import pytest

from app.services import AnomalyService, MetricService


GOLDEN_PATH = (
    Path(__file__).resolve().parents[2]
    / "dataset"
    / "outputs"
    / "10_黄金诊断集"
    / "01_黄金诊断案例_v1_0.csv"
)


def month_end(value: str) -> date:
    parsed = date.fromisoformat(value)
    return parsed.replace(day=monthrange(parsed.year, parsed.month)[1])


def golden_rows():
    with GOLDEN_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.parametrize("case", golden_rows(), ids=lambda row: row["case_id"])
def test_metric_compare_matches_frozen_golden_case(case):
    service = MetricService()
    baseline_start = date.fromisoformat(case["business_baseline_start"])
    baseline_end = month_end(case["business_baseline_end"])
    current_start = date.fromisoformat(case["current_month"])
    current_end = month_end(case["current_month"])

    orders = service.metric_compare(
        case["product_id"],
        "avg_daily_orders",
        current_start,
        current_end,
        baseline_start,
        baseline_end,
        "month",
    )
    refunds = service.metric_compare(
        case["product_id"],
        "refund_rate",
        current_start,
        current_end,
        baseline_start,
        baseline_end,
        "month",
    )

    assert orders["baseline_value"] == pytest.approx(float(case["baseline_avg_daily_orders"]), abs=1e-8)
    assert orders["current_value"] == pytest.approx(float(case["current_avg_daily_orders"]), abs=1e-8)
    assert orders["relative_change"] == pytest.approx(float(case["order_change_pct"]), abs=1e-8)
    assert refunds["baseline_value"] == pytest.approx(float(case["baseline_refund_rate"]), abs=1e-8)
    assert refunds["current_value"] == pytest.approx(float(case["current_refund_rate"]), abs=1e-8)
    assert refunds["absolute_delta"] == pytest.approx(float(case["refund_rate_delta"]), abs=1e-8)


@pytest.mark.parametrize("case", golden_rows(), ids=lambda row: row["case_id"])
def test_each_golden_case_has_a_detectable_business_anomaly(case):
    service = AnomalyService()
    result = service.detect_baseline(
        case["product_id"],
        "avg_daily_orders",
        date.fromisoformat(case["current_month"]),
        month_end(case["current_month"]),
        date.fromisoformat(case["business_baseline_start"]),
        month_end(case["business_baseline_end"]),
        "month",
    )
    assert result["is_anomaly"] is True
    assert result["data_source"] == "synthetic"

