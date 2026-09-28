from pathlib import Path

import pytest

from app.semantic.evaluator import SemanticEvaluator
from app.semantic.taxonomy import TaxonomyLoader


ROOT = Path(__file__).resolve().parents[2]
TAXONOMY = TaxonomyLoader.load(ROOT / "dataset/data/03_黄金评论集/主题分类体系_v1_1.json")


def claim(review_id, primary, secondary, polarity, evidence):
    return {
        "review_id": review_id,
        "primary_topic": primary,
        "secondary_topic": secondary,
        "polarity": polarity,
        "evidence_span": evidence,
    }


def test_evaluator_hand_calculated_multilabel_metrics():
    reviews = {"r1": "fast and hot", "r2": "slow"}
    golden = [
        claim("r1", "function_performance", "charging_speed", "positive", "fast"),
        claim("r1", "safety_risk", "overheating", "negative", "hot"),
        claim("r2", "function_performance", "charging_speed", "negative", "slow"),
    ]
    predicted = [
        claim("r1", "function_performance", "charging_speed", "positive", "fast"),
        claim("r1", "safety_risk", "overheating", "positive", "hot"),
        claim("r2", "function_performance", "charging_success", "negative", "missing"),
    ]
    report = SemanticEvaluator(TAXONOMY).evaluate(
        reviews=reviews, golden_claims=golden, predictions=predicted
    )

    # Secondary: 2 TP, 1 FP, 1 FN => 4 / 6.
    assert report.secondary_topic_micro_f1.value == pytest.approx(2 / 3)
    # Topic x polarity: 1 TP, 2 FP, 2 FN => 2 / 6.
    assert report.topic_polarity_micro_f1.value == pytest.approx(1 / 3)
    # Two matched review/topic pairs; one has the right polarity.
    assert report.matched_topic_polarity_accuracy.value == pytest.approx(0.5)
    assert report.evidence_substring_validity.value == pytest.approx(2 / 3)
    assert report.invalid_taxonomy_rate.value == 0
    assert report.unsupported_evidence_rate.value == pytest.approx(1 / 3)
    assert report.per_topic_support == {"charging_speed": 2, "overheating": 1}
    assert report.bad_cases[0]["reasons"] == ["unsupported_evidence"]


def test_evaluator_detects_illegal_taxonomy_pair():
    report = SemanticEvaluator(TAXONOMY).evaluate(
        reviews={"r1": "fast"},
        golden_claims=[],
        predictions=[claim("r1", "safety_risk", "charging_speed", "positive", "fast")],
    )
    assert report.invalid_taxonomy_rate.value == 1
    assert not report.invalid_taxonomy_rate.passed


def test_empty_predictions_have_safe_quality_rates_not_fake_baseline_scores():
    report = SemanticEvaluator(TAXONOMY).evaluate(
        reviews={"r1": "fast"},
        golden_claims=[claim("r1", "function_performance", "charging_speed", "positive", "fast")],
        predictions=[],
    )
    assert report.secondary_topic_micro_f1.value == 0
    assert report.evidence_substring_validity.value == 1
    assert report.invalid_taxonomy_rate.value == 0
    assert report.unsupported_evidence_rate.value == 0
