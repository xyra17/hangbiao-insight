from datetime import date

import pytest

from app.engines.metric_engine import MetricEngine


def row(day: str, orders: float, refunds: float):
    return {
        "date": date.fromisoformat(day),
        "order_count": orders,
        "refund_count": refunds,
        "refund_rate": refunds / orders,
        "is_injected": False,
        "scenario_id": "",
    }


def test_monthly_refund_rate_is_weighted_by_orders():
    points = MetricEngine().aggregate(
        [row("2024-01-01", 100, 10), row("2024-01-02", 10, 5)],
        "refund_rate",
        "month",
    )
    assert len(points) == 1
    assert points[0]["value"] == pytest.approx(15 / 110)
    assert points[0]["observation_days"] == 2


def test_week_starts_on_monday_and_average_daily_orders_is_derived():
    points = MetricEngine().aggregate(
        [row("2024-01-01", 100, 2), row("2024-01-02", 200, 4)],
        "avg_daily_orders",
        "week",
    )
    assert points[0]["period_start"] == "2024-01-01"
    assert points[0]["period_end"] == "2024-01-07"
    assert points[0]["value"] == 150


def test_invalid_metric_is_rejected():
    with pytest.raises(ValueError, match="不支持的指标"):
        MetricEngine().aggregate([row("2024-01-01", 100, 2)], "gmv", "month")

