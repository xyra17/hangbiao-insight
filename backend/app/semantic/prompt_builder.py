"""Prompt construction that cannot access Golden annotations."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .schemas import PROMPT_VERSION, ReviewInput, StructuredExtraction
from .taxonomy import Taxonomy


@dataclass(frozen=True)
class PromptPackage:
    system: str
    user: str
    response_schema: dict
    prompt_version: str = PROMPT_VERSION


class PromptBuilder:
    def __init__(self, taxonomy: Taxonomy, *, prompt_version: str = PROMPT_VERSION) -> None:
        self.taxonomy = taxonomy
        self.prompt_version = prompt_version

    def build(self, review: ReviewInput) -> PromptPackage:
        taxonomy_json = json.dumps(self.taxonomy.topic_structure, ensure_ascii=False)
        if self.prompt_version == "semantic-prompt-v1.2":
            guidance = (
                "Claim-first but evidence-conservative extraction. Extract only explicit user experience of the current product; never infer from rating, "
                "common sense, product category, generic adjectives, or context. One independent experience equals one claim; multiple independent experiences "
                "may yield multiple claims. Every claim MUST have a shortest-but-sufficient continuous substring copied exactly from SOURCE_TEXT that directly "
                "supports both its topic and polarity. Preserve negation, degree, time conditions, and applicable object. Do not rewrite, concatenate, complete, "
                "translate, or use words absent from SOURCE_TEXT. If sufficient direct evidence is unavailable, omit the claim: prefer omission over guessing. "
                "Choose the most specific secondary topic. general_performance is a last-resort fallback only when no more specific performance topic fits. "
                "Never map across primary topics without explicit evidence and a legal parent-secondary pair. Do not output a merely possible or related topic "
                "to improve recall. Judge polarity independently for each claim. Before returning, self-check taxonomy and parent-child legality, evidence existence, "
                "direct Topic support, direct Polarity support, specificity, negation/degree/time/object preservation, and absence of unsupported guesses."
            )
        elif self.prompt_version == "semantic-prompt-v1.1":
            guidance = (
                "Claim-first extraction: first identify ALL independent, text-supported facts about the current product, then assign one "
                "primary_topic, secondary_topic, polarity, and evidence_span to each fact. Never stop after one topic; check for additional "
                "experiences, problems, benefits, limitations, and neutral facts. Maximize completeness only with explicit evidence; never guess. "
                "Prefer the most specific secondary topic over general_performance: charges slowly=charging_speed, does not charge=charging_success, "
                "weak output=power_output, stopped working=stopped_working, intermittent=intermittent_failure, gets very hot=overheating, "
                "device damage=device_damage, case does not fit=case_compatibility, missing expected accessory=accessory_condition. "
                "Judge polarity independently per claim; multiple polarities may coexist. Every claim needs an exact continuous substring from SOURCE_TEXT "
                "that directly supports its topic and polarity. Only current-product experience counts; ignore historical products, other brands, generic "
                "advice, advertising, and speculation. Rating is not polarity. Before returning, self-check completeness, evidence substring/support, "
                "specificity, parent-child legality, and that no topic was added from rating or common sense."
            )
        else:
            guidance = (
                "Extract product-review semantic claims. A review may contain zero or multiple claims. For every claim choose exactly one valid "
                "primary/secondary taxonomy pair, independently determine polarity, and copy evidence_span as an exact continuous substring from SOURCE_TEXT. "
                "Do not infer sentiment from star rating. Do not invent evidence or topics."
            )
        system = f"{guidance} If no supported product claim exists, return an empty list. Taxonomy version: {self.taxonomy.version}. Taxonomy: {taxonomy_json}"
        # Only runtime-safe ReviewInput fields are serialized; Golden labels cannot enter this boundary.
        user = json.dumps(
            {
                "review_id": review.review_id,
                "product_id": review.product_id,
                "title": review.title,
                "source_text": review.source_text,
                "rating": review.rating,
                "created_at": review.created_at.isoformat() if review.created_at else None,
            },
            ensure_ascii=False,
        )
        response_schema = StructuredExtraction.model_json_schema()
        claim_properties = response_schema["$defs"]["ExtractedClaim"]["properties"]
        claim_properties["primary_topic"]["enum"] = list(self.taxonomy.primary_topics)
        claim_properties["secondary_topic"]["enum"] = list(self.taxonomy.secondary_topics)
        return PromptPackage(
            system=system,
            user=user,
            response_schema=response_schema,
            prompt_version=self.prompt_version,
        )
