"""Golden-set metrics for Stage 5.5A. No inference is performed here."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .schemas import EvaluationReport, MetricValue, SemanticPrediction
from .taxonomy import Taxonomy

THRESHOLDS = {
    "primary": 0.85,
    "secondary": 0.75,
    "topic_polarity": 0.70,
    "polarity_accuracy": 0.85,
    "evidence_validity": 1.0,
    "invalid_taxonomy": 0.0,
    "unsupported_evidence": 0.0,
}


def _claim_value(claim: Any, name: str) -> Any:
    return claim.get(name) if isinstance(claim, Mapping) else getattr(claim, name)


def _f1(tp: int, fp: int, fn: int) -> float:
    denominator = 2 * tp + fp + fn
    return (2 * tp / denominator) if denominator else 1.0


def _higher_metric(value: float, numerator: int, denominator: int, target: float) -> MetricValue:
    return MetricValue(
        value=value, numerator=numerator, denominator=denominator, target=target, passed=value >= target
    )


def _lower_metric(value: float, numerator: int, denominator: int, target: float) -> MetricValue:
    return MetricValue(
        value=value, numerator=numerator, denominator=denominator, target=target, passed=value <= target
    )


class SemanticEvaluator:
    """Set-based multilabel evaluator grouped by review_id."""

    def __init__(self, taxonomy: Taxonomy) -> None:
        self.taxonomy = taxonomy

    def evaluate(
        self,
        *,
        reviews: Mapping[str, str],
        golden_claims: Iterable[Any],
        predictions: Iterable[SemanticPrediction | Mapping[str, Any]],
    ) -> EvaluationReport:
        golden = list(golden_claims)
        predicted = list(predictions)

        gold_primary = self._sets(golden, ("primary_topic",))
        pred_primary = self._sets(predicted, ("primary_topic",))
        per_primary_f1: list[float] = []
        review_ids = set(reviews) | set(gold_primary) | set(pred_primary)
        for topic in self.taxonomy.primary_topics:
            gold_ids = {rid for rid in review_ids if (topic,) in gold_primary.get(rid, set())}
            pred_ids = {rid for rid in review_ids if (topic,) in pred_primary.get(rid, set())}
            tp = len(gold_ids & pred_ids)
            fp = len(pred_ids - gold_ids)
            fn = len(gold_ids - pred_ids)
            per_primary_f1.append(_f1(tp, fp, fn))
        primary_macro = sum(per_primary_f1) / len(per_primary_f1)

        sec_tp, sec_fp, sec_fn = self._micro_counts(
            self._sets(golden, ("secondary_topic",)), self._sets(predicted, ("secondary_topic",))
        )
        pair_tp, pair_fp, pair_fn = self._micro_counts(
            self._sets(golden, ("secondary_topic", "polarity")),
            self._sets(predicted, ("secondary_topic", "polarity")),
        )

        gold_topic_polarities = self._topic_polarities(golden)
        pred_topic_polarities = self._topic_polarities(predicted)
        matched = correct = 0
        for rid in set(gold_topic_polarities) | set(pred_topic_polarities):
            for topic in set(gold_topic_polarities.get(rid, {})) & set(pred_topic_polarities.get(rid, {})):
                matched += 1
                if gold_topic_polarities[rid][topic] == pred_topic_polarities[rid][topic]:
                    correct += 1
        polarity_accuracy = correct / matched if matched else 1.0

        evidence_valid = invalid_taxonomy = unsupported = 0
        bad_cases: list[dict[str, Any]] = []
        for claim in predicted:
            rid = str(_claim_value(claim, "review_id"))
            primary = str(_claim_value(claim, "primary_topic"))
            secondary = str(_claim_value(claim, "secondary_topic"))
            evidence = str(_claim_value(claim, "evidence_span") or "")
            source = reviews.get(rid, "")
            valid_topic = self.taxonomy.is_valid(primary, secondary)
            valid_evidence = bool(evidence) and evidence in source
            evidence_valid += int(valid_evidence)
            invalid_taxonomy += int(not valid_topic)
            unsupported += int(not valid_evidence)
            if not valid_topic or not valid_evidence:
                bad_cases.append(
                    {
                        "review_id": rid,
                        "primary_topic": primary,
                        "secondary_topic": secondary,
                        "evidence_span": evidence,
                        "reasons": [
                            *([] if valid_topic else ["invalid_taxonomy"]),
                            *([] if valid_evidence else ["unsupported_evidence"]),
                        ],
                    }
                )

        support = Counter(str(_claim_value(c, "secondary_topic")) for c in golden)
        pred_count = len(predicted)
        evidence_rate = evidence_valid / pred_count if pred_count else 1.0
        invalid_rate = invalid_taxonomy / pred_count if pred_count else 0.0
        unsupported_rate = unsupported / pred_count if pred_count else 0.0

        return EvaluationReport(
            primary_topic_macro_f1=_higher_metric(
                primary_macro, sum(per_primary_f1), len(per_primary_f1), THRESHOLDS["primary"]
            ),
            secondary_topic_micro_f1=_higher_metric(
                _f1(sec_tp, sec_fp, sec_fn), 2 * sec_tp, 2 * sec_tp + sec_fp + sec_fn, THRESHOLDS["secondary"]
            ),
            topic_polarity_micro_f1=_higher_metric(
                _f1(pair_tp, pair_fp, pair_fn),
                2 * pair_tp,
                2 * pair_tp + pair_fp + pair_fn,
                THRESHOLDS["topic_polarity"],
            ),
            matched_topic_polarity_accuracy=_higher_metric(
                polarity_accuracy, correct, matched, THRESHOLDS["polarity_accuracy"]
            ),
            evidence_substring_validity=_higher_metric(
                evidence_rate, evidence_valid, pred_count, THRESHOLDS["evidence_validity"]
            ),
            invalid_taxonomy_rate=_lower_metric(
                invalid_rate, invalid_taxonomy, pred_count, THRESHOLDS["invalid_taxonomy"]
            ),
            unsupported_evidence_rate=_lower_metric(
                unsupported_rate, unsupported, pred_count, THRESHOLDS["unsupported_evidence"]
            ),
            per_topic_support=dict(sorted(support.items())),
            bad_cases=bad_cases,
        )

    @staticmethod
    def _sets(claims: Iterable[Any], fields: tuple[str, ...]) -> dict[str, set[tuple[str, ...]]]:
        result: dict[str, set[tuple[str, ...]]] = defaultdict(set)
        for claim in claims:
            rid = str(_claim_value(claim, "review_id"))
            result[rid].add(tuple(str(_claim_value(claim, field)) for field in fields))
        return result

    @staticmethod
    def _micro_counts(gold: dict[str, set], predicted: dict[str, set]) -> tuple[int, int, int]:
        tp = fp = fn = 0
        for rid in set(gold) | set(predicted):
            expected = gold.get(rid, set())
            actual = predicted.get(rid, set())
            tp += len(expected & actual)
            fp += len(actual - expected)
            fn += len(expected - actual)
        return tp, fp, fn

    @staticmethod
    def _topic_polarities(claims: Iterable[Any]) -> dict[str, dict[str, frozenset[str]]]:
        raw: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for claim in claims:
            raw[str(_claim_value(claim, "review_id"))][str(_claim_value(claim, "secondary_topic"))].add(
                str(_claim_value(claim, "polarity"))
            )
        return {
            rid: {topic: frozenset(values) for topic, values in topics.items()} for rid, topics in raw.items()
        }
