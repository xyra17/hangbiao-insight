from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Literal


Granularity = Literal["day", "week", "month"]

METRIC_ALIASES = {
    "orders": "order_count",
    "refunds": "refund_count",
    "average_daily_orders": "avg_daily_orders",
}
SUPPORTED_METRICS = {"order_count", "refund_count", "refund_rate", "avg_daily_orders"}


def canonical_metric(metric_name: str) -> str:
    name = METRIC_ALIASES.get(metric_name, metric_name)
    if name not in SUPPORTED_METRICS:
        raise ValueError(f"不支持的指标：{metric_name}")
    return name


class MetricEngine:
    """纯确定性指标计算，不读取 Golden expected answer。"""

    def aggregate(
        self,
        rows: list[dict[str, Any]],
        metric_name: str,
        granularity: Granularity,
    ) -> list[dict[str, Any]]:
        metric = canonical_metric(metric_name)
        grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[self._period_start(row["date"], granularity)].append(row)

        points = []
        for period_start in sorted(grouped):
            period_rows = grouped[period_start]
            order_count = sum(row["order_count"] for row in period_rows)
            refund_count = sum(row["refund_count"] for row in period_rows)
            value = self._value(metric, order_count, refund_count, len(period_rows))
            points.append(
                {
                    "period_start": period_start.isoformat(),
                    "period_end": self._period_end(period_start, granularity).isoformat(),
                    "value": round(value, 10),
                    "observation_days": len(period_rows),
                    "order_count": round(order_count, 6),
                    "refund_count": round(refund_count, 6),
                    "is_injected": any(row["is_injected"] for row in period_rows),
                    "scenario_ids": sorted({row["scenario_id"] for row in period_rows if row.get("scenario_id")}),
                }
            )
        return points

    def summarize(
        self,
        rows: list[dict[str, Any]],
        metric_name: str,
        granularity: Granularity,
    ) -> float | None:
        if not rows:
            return None
        metric = canonical_metric(metric_name)
        if metric == "refund_rate":
            orders = sum(row["order_count"] for row in rows)
            return sum(row["refund_count"] for row in rows) / orders if orders else 0.0
        points = self.aggregate(rows, metric, granularity)
        if not points:
            return None
        return sum(point["value"] for point in points) / len(points)

    @staticmethod
    def _value(metric: str, orders: float, refunds: float, days: int) -> float:
        if metric == "order_count":
            return orders
        if metric == "refund_count":
            return refunds
        if metric == "refund_rate":
            return refunds / orders if orders else 0.0
        return orders / days if days else 0.0

    @staticmethod
    def _period_start(value: date, granularity: Granularity) -> date:
        if granularity == "day":
            return value
        if granularity == "week":
            return value - timedelta(days=value.weekday())
        if granularity == "month":
            return value.replace(day=1)
        raise ValueError(f"不支持的粒度：{granularity}")

    @staticmethod
    def _period_end(period_start: date, granularity: Granularity) -> date:
        if granularity == "day":
            return period_start
        if granularity == "week":
            return period_start + timedelta(days=6)
        return period_start.replace(day=monthrange(period_start.year, period_start.month)[1])

