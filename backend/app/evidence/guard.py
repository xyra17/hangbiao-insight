from __future__ import annotations

import re
from collections.abc import Iterable

from ..repositories.review_repository import ReviewRepository
from .schemas import EvidenceInput, EvidenceStatus, EvidenceValidation, SourceType, ValidatedEvidence


_CAUSAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (r"\bcaused? by\b", r"\bresults? in\b", r"\bbecause of\b", r"导致", r"造成", r"就是因为", r"原因是")
)


def _normalize(value: str) -> str:
    return " ".join(value.replace("<br />", " ").replace("<br/>", " ").split()).casefold()


class EvidenceGuard:
    """Validate provenance and quotes without claiming causal truth."""

    def __init__(self, reviews: ReviewRepository | None = None) -> None:
        self.reviews = reviews or ReviewRepository()

    def validate(
        self,
        *,
        claim: str,
        evidence: Iterable[EvidenceInput],
        allowed_source_refs: set[tuple[SourceType, str]],
    ) -> EvidenceValidation:
        unique: dict[tuple[str, str, str], EvidenceInput] = {}
        warnings: list[str] = []
        errors: list[str] = []
        for item in evidence:
            key = (str(item.source_type), item.source_id, _normalize(item.quote))
            if key in unique:
                warnings.append(f"duplicate_evidence:{item.source_id}")
                continue
            unique[key] = item
        if not unique:
            errors.append("evidence_required")

        validated: list[ValidatedEvidence] = []
        for item in unique.values():
            permitted = (item.source_type, item.source_id) in allowed_source_refs
            source_found = permitted
            quote_found = bool(item.quote.strip())
            if not permitted:
                errors.append(f"source_not_returned_by_tool:{item.source_type}:{item.source_id}")
            if item.source_type == SourceType.REVIEW:
                review = self.reviews.detail(item.source_id) if permitted else None
                source_found = review is not None
                source_text = f'{review.get("title", "")}\n{review.get("text", "")}' if review else ""
                quote_found = bool(item.quote.strip()) and _normalize(item.quote) in _normalize(source_text)
                if not source_found:
                    errors.append(f"review_not_found:{item.source_id}")
                elif not quote_found:
                    errors.append(f"quote_not_found:{item.source_id}")
            elif not item.quote.strip():
                errors.append(f"empty_quote:{item.source_type}:{item.source_id}")
            validated.append(
                ValidatedEvidence(**item.model_dump(), quote_found=quote_found, source_found=source_found)
            )

        causal = any(pattern.search(claim) for pattern in _CAUSAL_PATTERNS)
        if causal:
            warnings.append("causality_not_proven_by_evidence_guard")
            errors.append("causal_claim_requires_external_validation")
        conflict = any(item.stance == "contradict" for item in unique.values())
        if conflict:
            status = EvidenceStatus.CONFLICT
        elif errors:
            status = EvidenceStatus.INSUFFICIENT
        else:
            status = EvidenceStatus.VERIFIED
        return EvidenceValidation(
            passed=not errors,
            evidence_status=status,
            validated_evidence=validated,
            warnings=list(dict.fromkeys(warnings)),
            errors=list(dict.fromkeys(errors)),
        )
