"""Data contracts for the Stage 5.5A semantic extraction pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PROMPT_VERSION = "semantic-prompt-v1.0"
SEMANTIC_VERSION = "review-semantic-v1.0"


class Polarity(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    MIXED = "mixed"


class PredictionStatus(StrEnum):
    ACCEPTED = "accepted"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"


class ReviewInput(BaseModel):
    """Runtime-safe input. It deliberately has no Golden label fields."""

    model_config = ConfigDict(extra="forbid")

    review_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    title: str = ""
    text: str = Field(min_length=1)
    rating: float | None = Field(default=None, ge=0, le=5)
    created_at: datetime | None = None

    @property
    def source_text(self) -> str:
        if self.title and self.title not in self.text:
            return f"{self.title}\n{self.text}"
        return self.text


class ExtractedClaim(BaseModel):
    """The only fields the LLM is asked to produce for one claim."""

    model_config = ConfigDict(extra="forbid")

    primary_topic: str = Field(min_length=1)
    secondary_topic: str = Field(min_length=1)
    polarity: Polarity
    evidence_span: str = Field(min_length=1)


class StructuredExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[ExtractedClaim]


class GatewayResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: str | dict[str, Any]
    provider: str
    api_name: str
    model_name: str
    reasoning_effort: str
    prompt_version: str
    taxonomy_version: str
    raw_response_ref: str | None = None
    raw_response: dict[str, Any] | None = None


class SemanticPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prediction_id: str
    review_id: str
    product_id: str
    created_at: datetime | None = None
    primary_topic: str
    secondary_topic: str
    polarity: Polarity
    evidence_span: str
    prediction_status: PredictionStatus = PredictionStatus.ACCEPTED
    quality_flags: list[str] = Field(default_factory=list)
    provider: str
    api_name: str
    model_name: str
    reasoning_effort: str
    prompt_version: str = PROMPT_VERSION
    taxonomy_version: str
    semantic_version: str = SEMANTIC_VERSION
    raw_response_ref: str | None = None


class GuardResult(BaseModel):
    predictions: list[SemanticPrediction]
    accepted_count: int
    review_required_count: int
    rejected_count: int


class MetricValue(BaseModel):
    value: float
    numerator: int | float
    denominator: int | float
    target: float
    passed: bool


class EvaluationReport(BaseModel):
    primary_topic_macro_f1: MetricValue
    secondary_topic_micro_f1: MetricValue
    topic_polarity_micro_f1: MetricValue
    matched_topic_polarity_accuracy: MetricValue
    evidence_substring_validity: MetricValue
    invalid_taxonomy_rate: MetricValue
    unsupported_evidence_rate: MetricValue
    per_topic_support: dict[str, int]
    bad_cases: list[dict[str, Any]]
