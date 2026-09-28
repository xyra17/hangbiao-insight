from __future__ import annotations

from enum import Enum
from typing import Any


class BusinessState(str, Enum):
    HEALTHY = "healthy"
    IMPROVING = "improving"
    DETERIORATING = "deteriorating"
    MIXED = "mixed"
    STABLE = "stable"


PATTERN_NAMES = {
    "healthy_growth": "健康增长",
    "growth_with_refund_risk": "增长伴随售后风险",
    "growth_with_refund_caution": "增长伴随退款观察项",
    "broad_deterioration": "综合恶化",
    "demand_decline": "需求走弱",
    "positive_recovery": "经营恢复",
    "negative_reversal": "经营反转",
    "mixed_signal": "指标信号冲突",
    "stable": "整体稳定",
}


class MultiMetricEngine:
    """只消费结构化指标和异常结果的确定性模式分类器。"""

    VERSION = "multi-metric-v1.0"

    def analyze(
        self,
        product_id: str,
        period: str,
        metrics: dict[str, dict[str, Any]],
        anomalies: dict[str, dict[str, Any]],
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        order_name = "avg_daily_orders" if "avg_daily_orders" in metrics else "order_count"
        refund_name = "refund_rate" if "refund_rate" in metrics else "refund_count"
        order = self.build_signal(order_name, metrics, anomalies)
        refund = self.build_signal(refund_name, metrics, anomalies)
        current_state = self.infer_state(order, refund)
        previous_state = self._previous_state(history)
        pattern = self.classify(order, refund, current_state, previous_state)
        severity = self._severity(pattern, order, refund)
        primary, supporting, conflicting = self._signal_roles(pattern, order_name, refund_name)

        return {
            "product_id": product_id,
            "period": period,
            "business_state": current_state.value,
            "previous_business_state": previous_state.value if previous_state else None,
            "pattern": pattern,
            "pattern_name_zh": PATTERN_NAMES[pattern],
            "severity": severity,
            "business_direction": self._business_direction(pattern),
            "primary_signal": primary,
            "supporting_signals": supporting,
            "conflicting_signals": conflicting,
            "summary_zh": self._summary(pattern),
            "needs_investigation": pattern not in {"healthy_growth", "positive_recovery", "stable"},
            "recommended_investigation": self._recommendations(pattern),
            "metrics": metrics,
            "history": history or [],
            "source": "deterministic",
            "algorithm_version": self.VERSION,
        }

    def infer_state(self, order: dict[str, Any], refund: dict[str, Any]) -> BusinessState:
        order_direction = order["direction"]
        refund_direction = refund["direction"]
        if order_direction == "better" and refund_direction == "better":
            return BusinessState.IMPROVING
        if order_direction == "worse" and refund_direction == "worse":
            return BusinessState.DETERIORATING
        if order_direction != refund_direction and "stable" not in {order_direction, refund_direction}:
            return BusinessState.MIXED
        if not order["is_anomaly"] and not refund["is_anomaly"]:
            return BusinessState.STABLE
        if order_direction == "better" or refund_direction == "better":
            return BusinessState.HEALTHY
        return BusinessState.STABLE

    def classify(
        self,
        order: dict[str, Any],
        refund: dict[str, Any],
        current_state: BusinessState,
        previous_state: BusinessState | None,
    ) -> str:
        order_up = order["direction"] == "better" and order["is_anomaly"]
        order_down = order["direction"] == "worse" and order["is_anomaly"]
        refund_worse = refund["direction"] == "worse"
        refund_worse_anomaly = refund_worse and refund["is_anomaly"]
        refund_better = refund["direction"] == "better"

        if previous_state == BusinessState.IMPROVING and current_state == BusinessState.DETERIORATING:
            return "negative_reversal"
        if order_up and refund_worse_anomaly:
            return "growth_with_refund_risk"
        if order_up and refund_worse:
            return "growth_with_refund_caution"
        if order_down and refund_worse_anomaly:
            return "broad_deterioration"
        if order_down and not refund_worse_anomaly:
            return "demand_decline"
        if current_state == BusinessState.IMPROVING and previous_state in {
            BusinessState.DETERIORATING,
            BusinessState.MIXED,
            BusinessState.STABLE,
        }:
            return "positive_recovery"
        if order_up and (refund_better or refund["direction"] == "stable"):
            return "healthy_growth"
        if not order["is_anomaly"] and not refund["is_anomaly"]:
            return "stable"
        return "mixed_signal"

    @staticmethod
    def build_signal(name: str, metrics: dict[str, Any], anomalies: dict[str, Any]) -> dict[str, Any]:
        metric = metrics[name]
        anomaly = anomalies[name]
        return {
            "direction": anomaly["direction"],
            "is_anomaly": anomaly["is_anomaly"],
            "severity": anomaly["severity"],
            "absolute_delta": metric.get("absolute_delta"),
            "relative_change": metric.get("relative_change"),
        }

    @staticmethod
    def _previous_state(history: list[dict[str, Any]] | None) -> BusinessState | None:
        if not history:
            return None
        value = history[-1].get("business_state")
        return BusinessState(value) if value else None

    @staticmethod
    def _severity(pattern: str, order: dict[str, Any], refund: dict[str, Any]) -> str:
        if pattern in {"negative_reversal", "growth_with_refund_risk", "broad_deterioration"}:
            return "high"
        if pattern == "stable":
            return "low"
        if "high" in {order["severity"], refund["severity"]}:
            return "high"
        return "medium"

    @staticmethod
    def _signal_roles(pattern: str, order: str, refund: str) -> tuple[str, list[str], list[str]]:
        if pattern in {"growth_with_refund_risk", "growth_with_refund_caution"}:
            return refund, [order], [order, refund]
        if pattern in {"broad_deterioration", "negative_reversal"}:
            return refund, [order, refund], []
        if pattern in {"demand_decline", "healthy_growth", "positive_recovery"}:
            return order, [order, refund], []
        return order, [order, refund], [order, refund] if pattern == "mixed_signal" else []

    @staticmethod
    def _business_direction(pattern: str) -> str:
        if pattern in {"healthy_growth", "positive_recovery"}:
            return "better"
        if pattern in {"broad_deterioration", "demand_decline", "negative_reversal"}:
            return "worse"
        if pattern == "stable":
            return "stable"
        return "mixed"

    @staticmethod
    def _summary(pattern: str) -> str:
        summaries = {
            "healthy_growth": "订单量显著增长，退款率保持稳定或改善，当前增长质量健康。",
            "growth_with_refund_risk": "订单量显著增长，但退款率同步恶化，当前增长质量存在售后风险。",
            "growth_with_refund_caution": "订单增长明显，但退款率没有同步改善，整体偏正向但存在售后侧观察项。",
            "broad_deterioration": "订单量显著下降且退款率恶化，经营表现与售后质量同时承压。",
            "demand_decline": "订单量显著下降，但退款率未明显恶化，当前更接近需求侧走弱。",
            "positive_recovery": "经营状态由此前承压或混合转为改善，订单增长并伴随退款侧稳定或改善。",
            "negative_reversal": "经营状态由改善转为恶化，订单下降并伴随退款率上升。",
            "mixed_signal": "多个经营指标方向不一致，暂时无法归入明确经营模式。",
            "stable": "订单和退款指标均未明显偏离近期基线，整体经营状态稳定。",
        }
        return summaries[pattern]

    @staticmethod
    def _recommendations(pattern: str) -> list[str]:
        recommendations = {
            "growth_with_refund_risk": ["review_quality", "refund_reason"],
            "growth_with_refund_caution": ["refund_reason", "post_purchase_quality"],
            "broad_deterioration": ["demand_driver", "refund_reason", "review_quality"],
            "demand_decline": ["traffic_conversion", "price_promotion"],
            "negative_reversal": ["recent_change", "refund_reason", "review_quality"],
            "mixed_signal": ["metric_breakdown", "data_quality"],
            "healthy_growth": [],
            "positive_recovery": [],
            "stable": [],
        }
        return recommendations[pattern]
