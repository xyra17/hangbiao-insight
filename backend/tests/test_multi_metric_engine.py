import pytest

from app.engines.multi_metric_engine import BusinessState, MultiMetricEngine


def signal(direction: str, is_anomaly: bool, severity: str = "medium"):
    return {"direction": direction, "is_anomaly": is_anomaly, "severity": severity}


def inputs(order, refund):
    metrics = {
        "avg_daily_orders": {"current": 120, "baseline": 100, "relative_change": 0.2},
        "refund_rate": {"current": 0.05, "baseline": 0.04, "absolute_delta": 0.01},
    }
    anomalies = {"avg_daily_orders": order, "refund_rate": refund}
    return metrics, anomalies


@pytest.mark.parametrize(
    ("order", "refund", "history", "pattern"),
    [
        (signal("better", True), signal("better", False, "low"), None, "healthy_growth"),
        (signal("better", True), signal("worse", True, "high"), None, "growth_with_refund_risk"),
        (signal("better", True), signal("worse", False, "low"), None, "growth_with_refund_caution"),
        (signal("worse", True), signal("worse", True, "high"), None, "broad_deterioration"),
        (signal("worse", True), signal("stable", False, "low"), None, "demand_decline"),
        (
            signal("better", True),
            signal("better", True),
            [{"business_state": BusinessState.DETERIORATING.value}],
            "positive_recovery",
        ),
        (
            signal("worse", True),
            signal("worse", True, "high"),
            [{"business_state": BusinessState.IMPROVING.value}],
            "negative_reversal",
        ),
        (signal("better", False, "low"), signal("worse", True), None, "mixed_signal"),
        (signal("stable", False, "low"), signal("stable", False, "low"), None, "stable"),
    ],
)
def test_pattern_classifier(order, refund, history, pattern):
    metrics, anomalies = inputs(order, refund)
    result = MultiMetricEngine().analyze("P1", "2024-01", metrics, anomalies, history)
    assert result["pattern"] == pattern
    assert result["source"] == "deterministic"
    assert result["algorithm_version"] == "multi-metric-v1.0"


def test_growth_with_refund_risk_exposes_conflict_and_next_investigation():
    metrics, anomalies = inputs(signal("better", True), signal("worse", True, "high"))
    result = MultiMetricEngine().analyze("P1", "2024-01", metrics, anomalies)
    assert result["primary_signal"] == "refund_rate"
    assert result["conflicting_signals"] == ["avg_daily_orders", "refund_rate"]
    assert result["recommended_investigation"] == ["review_quality", "refund_reason"]
    assert "导致" not in result["summary_zh"]

