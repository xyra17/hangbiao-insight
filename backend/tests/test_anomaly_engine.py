from app.engines.anomaly_engine import AnomalyEngine
from app.services.anomaly_service import AnomalyService


def test_baseline_delta_returns_chinese_reason_and_direction():
    result = AnomalyEngine().baseline_delta("refund_rate", 0.061, 0.039)
    assert result["is_anomaly"] is True
    assert result["direction"] == "worse"
    assert result["severity"] == "high"
    assert result["reason"] == "退款率较近期基线显著恶化"


def test_statistical_detectors_are_deterministic():
    engine = AnomalyEngine()
    assert engine.rolling_zscore("order_count", [100, 102, 98, 101], 60)["is_anomaly"]
    assert engine.robust_zscore("refund_rate", [0.03, 0.031, 0.029, 0.03], 0.08)["is_anomaly"]
    change = engine.change_point("order_count", [100, 102, 99, 60, 58, 61])
    assert change["direction"] == "worse"


def test_auto_method_routes_refund_rate_to_baseline_and_robust_z():
    assert AnomalyService._select_methods("refund_rate", "auto") == ["baseline_delta", "robust_zscore"]
    assert AnomalyService._select_methods("order_count", "auto") == ["baseline_delta", "rolling_zscore"]
