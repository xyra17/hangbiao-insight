from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SourceType(StrEnum):
    REVIEW = "review"
    METRIC = "metric"
    ANOMALY = "anomaly"
    SYSTEM = "system"


class EvidenceStatus(StrEnum):
    VERIFIED = "verified"
    INSUFFICIENT = "insufficient"
    CONFLICT = "conflict"


class EvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    source_id: str = Field(min_length=1)
    quote: str = ""
    stance: str = "support"


class ValidatedEvidence(EvidenceInput):
    quote_found: bool
    source_found: bool


class EvidenceValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    evidence_status: EvidenceStatus
    validated_evidence: list[ValidatedEvidence]
    warnings: list[str]
    errors: list[str]
