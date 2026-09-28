from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import Any

from ..engines.multi_metric_engine import MultiMetricEngine
from .anomaly_service import AnomalyService
from .metric_service import MetricService


class MultiMetricService:
    P0_METRICS = ("order_count", "avg_daily_orders", "refund_count", "refund_rate")

    def __init__(
        self,
        metrics: MetricService | None = None,
        anomalies: AnomalyService | None = None,
        engine: MultiMetricEngine | None = None,
    ):
        self.metrics = metrics or MetricService()
        self.anomalies = anomalies or AnomalyService(metrics=self.metrics)
        self.engine = engine or MultiMetricEngine()

    def analyze_product(self, product_id: str, period: str, baseline_months: int = 3) -> dict[str, Any]:
        current_start = self._parse_period(period)
        current_end = self._month_end(current_start)
        baseline_start = self._shift_month(current_start, -baseline_months)
        baseline_end = self._month_end(self._shift_month(current_start, -1))
        metrics, anomalies = self._build_inputs(
            product_id, current_start, current_end, baseline_start, baseline_end
        )

        previous_start = self._shift_month(current_start, -1)
        previous_baseline_start = self._shift_month(previous_start, -baseline_months)
        previous_baseline_end = self._month_end(self._shift_month(previous_start, -1))
        previous_metrics, previous_anomalies = self._build_inputs(
            product_id,
            previous_start,
            self._month_end(previous_start),
            previous_baseline_start,
            previous_baseline_end,
        )
        order = self.engine.build_signal("avg_daily_orders", previous_metrics, previous_anomalies)
        refund = self.engine.build_signal("refund_rate", previous_metrics, previous_anomalies)
        history = [
            {
                "period": previous_start.strftime("%Y-%m"),
                "business_state": self.engine.infer_state(order, refund).value,
            }
        ]
        return self.engine.analyze(
            product_id,
            current_start.strftime("%Y-%m"),
            metrics,
            anomalies,
            history,
        )

    def _build_inputs(
        self,
        product_id: str,
        current_start: date,
        current_end: date,
        baseline_start: date,
        baseline_end: date,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        metrics: dict[str, Any] = {}
        anomalies: dict[str, Any] = {}
        for metric_name in self.P0_METRICS:
            detection = self.anomalies.detect_baseline(
                product_id,
                metric_name,
                current_start,
                current_end,
                baseline_start,
                baseline_end,
                "month",
            )
            metrics[metric_name] = {
                "current": detection["current_value"],
                "baseline": detection["baseline_value"],
                "absolute_delta": detection["absolute_delta"],
                "relative_change": detection["relative_change"],
                "direction": detection["direction"],
            }
            anomalies[metric_name] = {
                "is_anomaly": detection["is_anomaly"],
                "severity": detection["severity"],
                "direction": detection["direction"],
                "method": detection["method"],
                "score": detection["score"],
                "reason": detection["reason"],
            }
        return metrics, anomalies

    @staticmethod
    def _parse_period(period: str) -> date:
        try:
            return date.fromisoformat(f"{period}-01")
        except ValueError as exc:
            raise ValueError("period 必须使用 YYYY-MM 格式") from exc

    @staticmethod
    def _shift_month(value: date, months: int) -> date:
        index = value.year * 12 + value.month - 1 + months
        return date(index // 12, index % 12 + 1, 1)

    @staticmethod
    def _month_end(value: date) -> date:
        return value.replace(day=monthrange(value.year, value.month)[1])
