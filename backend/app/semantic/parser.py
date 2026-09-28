"""Strict parsing and deterministic prediction metadata enrichment."""

from __future__ import annotations

import hashlib
import json

from pydantic import ValidationError

from .schemas import GatewayResponse, ReviewInput, SemanticPrediction, StructuredExtraction


class SemanticParseError(ValueError):
    pass


class SemanticParser:
    def parse(self, review: ReviewInput, response: GatewayResponse) -> list[SemanticPrediction]:
        try:
            raw = json.loads(response.payload) if isinstance(response.payload, str) else response.payload
            extraction = StructuredExtraction.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise SemanticParseError(f"Invalid structured extraction: {exc}") from exc

        predictions: list[SemanticPrediction] = []
        for index, claim in enumerate(extraction.claims, start=1):
            digest = hashlib.sha256(
                f"{review.review_id}|{index}|{claim.primary_topic}|{claim.secondary_topic}|"
                f"{claim.polarity}|{claim.evidence_span}".encode()
            ).hexdigest()[:16]
            predictions.append(
                SemanticPrediction(
                    prediction_id=f"SP-{digest}",
                    review_id=review.review_id,
                    product_id=review.product_id,
                    created_at=review.created_at,
                    primary_topic=claim.primary_topic,
                    secondary_topic=claim.secondary_topic,
                    polarity=claim.polarity,
                    evidence_span=claim.evidence_span,
                    provider=response.provider,
                    api_name=response.api_name,
                    model_name=response.model_name,
                    reasoning_effort=response.reasoning_effort,
                    prompt_version=response.prompt_version,
                    taxonomy_version=response.taxonomy_version,
                    raw_response_ref=response.raw_response_ref,
                )
            )
        return predictions
