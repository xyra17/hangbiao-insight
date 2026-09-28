import pytest

from app.services import MultiMetricService


@pytest.mark.parametrize(
    ("product_id", "period", "expected_pattern"),
    [
        ("B07P9S6R9Y", "2020-06", "broad_deterioration"),
        ("B07P9S6R9Y", "2022-02", "growth_with_refund_risk"),
        ("B0BYJ8ZVWH", "2022-07", "positive_recovery"),
        ("B0BYJ8ZVWH", "2022-08", "negative_reversal"),
        ("B07H2V5YLH", "2022-11", "positive_recovery"),
        ("B07H2V5YLH", "2022-12", "growth_with_refund_caution"),
    ],
)
def test_golden_business_patterns(product_id, period, expected_pattern):
    result = MultiMetricService().analyze_product(product_id, period)
    assert result["pattern"] == expected_pattern
    assert result["source"] == "deterministic"
    assert result["algorithm_version"] == "multi-metric-v1.0"


def test_ailun_december_does_not_claim_all_metrics_improved():
    result = MultiMetricService().analyze_product("B07H2V5YLH", "2022-12")
    assert result["pattern"] == "growth_with_refund_caution"
    assert result["metrics"]["refund_rate"]["direction"] == "worse"
    assert result["metrics"]["refund_rate"]["absolute_delta"] > 0
    assert "全面改善" not in result["summary_zh"]
