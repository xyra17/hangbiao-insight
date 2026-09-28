from __future__ import annotations

from statistics import mean, median, pstdev
from typing import Any


class AnomalyEngine:
    """确定性异常算法。阈值集中管理，便于评测和版本化。"""

    VERSION = "anomaly-engine-v1.0"
    THRESHOLDS = {
        "order_count": {"relative": 0.20, "absolute": 0.0},
        "avg_daily_orders": {"relative": 0.20, "absolute": 0.0},
        "refund_count": {"relative": 0.20, "absolute": 0.0},
        "refund_rate": {"relative": 0.0, "absolute": 0.005},
    }
    LOWER_IS_BETTER = {"refund_count", "refund_rate"}

    def baseline_delta(self, metric_name: str, current: float, baseline: float) -> dict[str, Any]:
        absolute_delta = current - baseline
        relative_change = absolute_delta / abs(baseline) if baseline else None
        threshold = self.THRESHOLDS[metric_name]
        if threshold["absolute"]:
            score = abs(absolute_delta) / threshold["absolute"]
        else:
            score = abs(relative_change or 0.0) / threshold["relative"]
        return self._result(
            metric_name,
            "baseline_delta",
            score,
            current,
            baseline,
            absolute_delta,
            relative_change,
        )

    def rolling_zscore(self, metric_name: str, history: list[float], current: float) -> dict[str, Any]:
        center = mean(history) if history else current
        spread = pstdev(history) if len(history) > 1 else 0.0
        score = abs((current - center) / spread) if spread else (float("inf") if current != center else 0.0)
        return self._result(metric_name, "rolling_zscore", score, current, center, current - center, None)

    def robust_zscore(self, metric_name: str, history: list[float], current: float) -> dict[str, Any]:
        center = median(history) if history else current
        deviations = [abs(value - center) for value in history]
        mad = median(deviations) if deviations else 0.0
        score = abs(0.6745 * (current - center) / mad) if mad else (float("inf") if current != center else 0.0)
        return self._result(metric_name, "robust_zscore", score, current, center, current - center, None)

    def change_point(self, metric_name: str, values: list[float]) -> dict[str, Any]:
        if len(values) < 4:
            current = values[-1] if values else 0.0
            return self._result(metric_name, "change_point", 0.0, current, current, 0.0, None)
        split = len(values) // 2
        before, after = values[:split], values[split:]
        baseline, current = mean(before), mean(after)
        spread = pstdev(values) or 0.0
        score = abs(current - baseline) / spread if spread else (float("inf") if current != baseline else 0.0)
        return self._result(metric_name, "change_point", score, current, baseline, current - baseline, None)

    def _result(
        self,
        metric_name: str,
        method: str,
        score: float,
        current: float,
        baseline: float,
        absolute_delta: float,
        relative_change: float | None,
    ) -> dict[str, Any]:
        is_anomaly = score >= 1.0 if method == "baseline_delta" else score >= 2.0
        worse = absolute_delta > 0 if metric_name in self.LOWER_IS_BETTER else absolute_delta < 0
        direction = "worse" if worse else "better" if absolute_delta else "stable"
        severity = "high" if score >= 2.5 else "medium" if is_anomaly else "low"
        finite_score = round(score, 4) if score != float("inf") else 999.0
        return {
            "is_anomaly": is_anomaly,
            "severity": severity,
            "direction": direction,
            "method": method,
            "score": finite_score,
            "current_value": round(current, 10),
            "baseline_value": round(baseline, 10),
            "absolute_delta": round(absolute_delta, 10),
            "relative_change": round(relative_change, 10) if relative_change is not None else None,
            "reason": self._reason(metric_name, direction, is_anomaly),
            "engine_version": self.VERSION,
        }

    @staticmethod
    def _reason(metric_name: str, direction: str, is_anomaly: bool) -> str:
        labels = {
            "order_count": "订单量",
            "avg_daily_orders": "日均订单量",
            "refund_count": "退款量",
            "refund_rate": "退款率",
        }
        if not is_anomaly:
            return f"{labels[metric_name]}仍在近期基线范围内"
        movement = "显著恶化" if direction == "worse" else "显著改善"
        return f"{labels[metric_name]}较近期基线{movement}"

