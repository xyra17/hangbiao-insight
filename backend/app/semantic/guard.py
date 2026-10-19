"""Deterministic taxonomy, evidence, and duplicate guards."""

from __future__ import annotations

from .schemas import GuardResult, PredictionStatus, ReviewInput, SemanticPrediction
from .taxonomy import Taxonomy


class SemanticGuard:
    def __init__(self, taxonomy: Taxonomy) -> None:
        self.taxonomy = taxonomy

    def apply(self, review: ReviewInput, predictions: list[SemanticPrediction]) -> GuardResult:
        seen: set[tuple[str, str, str, str]] = set()
        guarded: list[SemanticPrediction] = []
        for prediction in predictions:
            flags: list[str] = []
            if prediction.review_id != review.review_id or prediction.product_id != review.product_id:
                flags.append("identity_mismatch")
            if not self.taxonomy.is_valid(prediction.primary_topic, prediction.secondary_topic):
                flags.append("invalid_topic")
            if not prediction.evidence_span or prediction.evidence_span not in review.source_text:
                flags.append("evidence_not_found")
            duplicate_key = (
                prediction.primary_topic,
                prediction.secondary_topic,
                str(prediction.polarity),
                prediction.evidence_span,
            )
            if duplicate_key in seen:
                flags.append("duplicate_claim")
            seen.add(duplicate_key)

            if "identity_mismatch" in flags or "invalid_topic" in flags or "evidence_not_found" in flags:
                status = PredictionStatus.REJECTED
            elif "duplicate_claim" in flags:
                status = PredictionStatus.REJECTED
            else:
                status = PredictionStatus.ACCEPTED
            guarded.append(
                prediction.model_copy(update={"quality_flags": flags, "prediction_status": status})
            )

        return GuardResult(
            predictions=guarded,
            accepted_count=sum(p.prediction_status == PredictionStatus.ACCEPTED for p in guarded),
            review_required_count=sum(
                p.prediction_status == PredictionStatus.REVIEW_REQUIRED for p in guarded
            ),
            rejected_count=sum(p.prediction_status == PredictionStatus.REJECTED for p in guarded),
        )
