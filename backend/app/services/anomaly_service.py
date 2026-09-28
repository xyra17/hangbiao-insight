from __future__ import annotations

from datetime import date
from typing import Any

from ..engines.anomaly_engine import AnomalyEngine
from ..services.metric_service import MetricService


class AnomalyService:
    def __init__(self, metrics: MetricService | None = None, engine: AnomalyEngine | None = None):
        self.metrics = metrics or MetricService()
        self.engine = engine or AnomalyEngine()

    def detect_baseline(
        self,
        product_id: str,
        metric_name: str,
        current_start: date,
        current_end: date,
        baseline_start: date,
        baseline_end: date,
        granularity: str = "month",
    ) -> dict[str, Any]:
        comparison = self.metrics.metric_compare(
            product_id,
            metric_name,
            current_start,
            current_end,
            baseline_start,
            baseline_end,
            granularity,
        )
        if comparison["current_value"] is None or comparison["baseline_value"] is None:
            return {**comparison, "is_anomaly": False, "reason": "指定时间范围内没有足够数据"}
        detection = self.engine.baseline_delta(
            comparison["metric_name"],
            comparison["current_value"],
            comparison["baseline_value"],
        )
        return {**comparison, **detection}

    def detect(
        self,
        product_id: str,
        metric_name: str,
        current_start: date,
        current_end: date,
        baseline_start: date,
        baseline_end: date,
        granularity: str = "month",
        method: str = "auto",
    ) -> dict[str, Any]:
        comparison = self.metrics.metric_compare(
            product_id,
            metric_name,
            current_start,
            current_end,
            baseline_start,
            baseline_end,
            granularity,
        )
        current_value = comparison["current_value"]
        baseline_value = comparison["baseline_value"]
        if current_value is None or baseline_value is None:
            return {
                **comparison,
                "is_anomaly": False,
                "severity": "low",
                "direction": "stable",
                "method": method,
                "detections": [],
                "reason": "指定时间范围内没有足够数据",
            }

        trend = self.metrics.metric_trend(
            product_id,
            metric_name,
            baseline_start,
            current_end,
            granularity,
        )
        history = [
            point["value"]
            for point in trend["points"]
            if point["period_start"] < current_start.isoformat()
        ]
        all_values = [point["value"] for point in trend["points"]]
        selected_methods = self._select_methods(comparison["metric_name"], method)
        detections = []
        for selected in selected_methods:
            if selected == "baseline_delta":
                result = self.engine.baseline_delta(comparison["metric_name"], current_value, baseline_value)
            elif selected == "rolling_zscore":
                result = self.engine.rolling_zscore(comparison["metric_name"], history, current_value)
            elif selected == "robust_zscore":
                result = self.engine.robust_zscore(comparison["metric_name"], history, current_value)
            else:
                result = self.engine.change_point(comparison["metric_name"], all_values)
            detections.append(result)

        primary = max(detections, key=lambda item: item["score"])
        anomaly = any(item["is_anomaly"] for item in detections)
        severity_rank = {"low": 0, "medium": 1, "high": 2}
        severity = max(detections, key=lambda item: severity_rank[item["severity"]])["severity"]
        return {
            **comparison,
            "is_anomaly": anomaly,
            "severity": severity,
            "direction": detections[0]["direction"],
            "method": method,
            "primary_method": primary["method"],
            "score": primary["score"],
            "reason": detections[0]["reason"],
            "detections": detections,
            "engine_version": self.engine.VERSION,
        }

    @staticmethod
    def _select_methods(metric_name: str, method: str) -> list[str]:
        supported = {"baseline_delta", "rolling_zscore", "robust_zscore", "change_point"}
        if method != "auto":
            if method not in supported:
                raise ValueError(f"不支持的异常检测方法：{method}")
            return [method]
        if metric_name == "refund_rate":
            return ["baseline_delta", "robust_zscore"]
        return ["baseline_delta", "rolling_zscore"]
