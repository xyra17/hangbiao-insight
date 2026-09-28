import json
from pathlib import Path

import pytest

from app.semantic.guard import SemanticGuard
from app.semantic.config import ProviderConfig
from app.semantic.model_gateway import ModelGateway
from app.semantic.parser import SemanticParseError, SemanticParser
from app.semantic.prompt_builder import PromptBuilder
from app.semantic.runner import SemanticExtractionService
from app.semantic.schemas import (
    GatewayResponse,
    Polarity,
    PredictionStatus,
    ReviewInput,
)
from app.semantic.taxonomy import TaxonomyLoader


ROOT = Path(__file__).resolve().parents[2]
TAXONOMY_PATH = ROOT / "dataset/data/03_黄金评论集/主题分类体系_v1_1.json"


class FakeGateway(ModelGateway):
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    @property
    def config(self):
        return ProviderConfig(provider="lmstudio", model="fake-structured-model", api_name="chat_completions_compatible")

    def extract(self, review, prompt):
        return response(self.payload)


def response(payload):
    return GatewayResponse(
        payload=payload,
        provider="lmstudio",
        api_name="chat_completions_compatible",
        model_name="fake-structured-model",
        reasoning_effort="low",
        prompt_version="semantic-prompt-v1.0",
        taxonomy_version="taxonomy-v1.1",
        raw_response_ref="fake-1",
    )


@pytest.fixture
def taxonomy():
    return TaxonomyLoader.load(TAXONOMY_PATH)


def test_taxonomy_loads_frozen_v11(taxonomy):
    assert taxonomy.version == "taxonomy-v1.1"
    assert len(taxonomy.primary_topics) == 8
    # The frozen file is authoritative: 5+6+6+6+6+4+3+2 = 38.
    assert len(taxonomy.secondary_topics) == 38
    assert taxonomy.is_valid("function_performance", "charging_speed")
    assert not taxonomy.is_valid("safety_risk", "charging_speed")


def test_prompt_contains_taxonomy_but_not_golden_labels_and_rating_is_not_sentiment(taxonomy):
    review = ReviewInput(
        review_id="r1",
        product_id="p1",
        title="Works",
        text="Works, but gets hot.",
        rating=5,
        created_at=None,
    )
    prompt = PromptBuilder(taxonomy).build(review)
    body = json.loads(prompt.user)
    assert body["created_at"] is None
    assert set(body) == {"review_id", "product_id", "title", "source_text", "rating", "created_at"}
    assert "Do not infer sentiment from star rating" in prompt.system
    assert "final_1_primary" not in prompt.user
    assert "taxonomy-v1.1" in prompt.system
    properties = prompt.response_schema["$defs"]["ExtractedClaim"]["properties"]
    assert set(properties["primary_topic"]["enum"]) == set(taxonomy.primary_topics)
    assert set(properties["secondary_topic"]["enum"]) == set(taxonomy.secondary_topics)


def test_prompt_v11_has_claim_first_completeness_and_specificity_guidance(taxonomy):
    prompt = PromptBuilder(taxonomy, prompt_version="semantic-prompt-v1.1").build(
        ReviewInput(review_id="r1", product_id="p1", text="Charges slowly but never stops.")
    )
    assert prompt.prompt_version == "semantic-prompt-v1.1"
    assert "Claim-first extraction" in prompt.system
    assert "Never stop" in prompt.system
    assert "charging_speed" in prompt.system
    assert "Rating is not polarity" in prompt.system


def test_prompt_v12_is_evidence_conservative(taxonomy):
    prompt = PromptBuilder(taxonomy, prompt_version="semantic-prompt-v1.2").build(
        ReviewInput(review_id="r1", product_id="p1", text="It is nice but not fast.")
    )
    assert prompt.prompt_version == "semantic-prompt-v1.2"
    assert "shortest-but-sufficient continuous substring" in prompt.system
    assert "prefer omission over guessing" in prompt.system
    assert "general_performance is a last-resort fallback" in prompt.system
    assert "Never map across primary topics" in prompt.system


def test_fake_gateway_parser_and_guard_support_multiple_claims_and_mixed(taxonomy):
    review = ReviewInput(review_id="r1", product_id="p1", text="Charges fast but gets very hot.", rating=5)
    gateway = FakeGateway(
        {"claims": [
            {"primary_topic": "function_performance", "secondary_topic": "charging_speed",
             "polarity": "mixed", "evidence_span": "Charges fast"},
            {"primary_topic": "safety_risk", "secondary_topic": "overheating",
             "polarity": "negative", "evidence_span": "gets very hot"},
        ]}
    )
    prompt = PromptBuilder(taxonomy).build(review)
    parsed = SemanticParser().parse(review, gateway.extract(review, prompt))
    result = SemanticGuard(taxonomy).apply(review, parsed)
    assert [p.polarity for p in result.predictions] == [Polarity.MIXED, Polarity.NEGATIVE]
    assert result.accepted_count == 2
    assert all(p.prediction_status == PredictionStatus.ACCEPTED for p in result.predictions)


def test_guard_rejects_invalid_parent_child_and_missing_evidence_and_flags_duplicate(taxonomy):
    review = ReviewInput(review_id="r1", product_id="p1", text="Charges fast.")
    payload = {"claims": [
        {"primary_topic": "safety_risk", "secondary_topic": "charging_speed",
         "polarity": "positive", "evidence_span": "Charges fast"},
        {"primary_topic": "function_performance", "secondary_topic": "charging_speed",
         "polarity": "positive", "evidence_span": "not in source"},
        {"primary_topic": "function_performance", "secondary_topic": "charging_speed",
         "polarity": "positive", "evidence_span": "Charges fast"},
        {"primary_topic": "function_performance", "secondary_topic": "charging_speed",
         "polarity": "positive", "evidence_span": "Charges fast"},
    ]}
    parsed = SemanticParser().parse(review, response(payload))
    result = SemanticGuard(taxonomy).apply(review, parsed)
    assert result.predictions[0].quality_flags == ["invalid_topic"]
    assert result.predictions[0].prediction_status == PredictionStatus.REJECTED
    assert result.predictions[1].quality_flags == ["evidence_not_found"]
    assert result.predictions[1].prediction_status == PredictionStatus.REJECTED
    assert result.predictions[3].quality_flags == ["duplicate_claim"]
    assert result.predictions[3].prediction_status == PredictionStatus.REJECTED


def test_extraction_service_connects_gateway_parser_and_guard(taxonomy):
    review = ReviewInput(review_id="r1", product_id="p1", text="Easy to use.", rating=1)
    service = SemanticExtractionService(
        prompt_builder=PromptBuilder(taxonomy),
        gateway=FakeGateway({"claims": [{
            "primary_topic": "usability_installation",
            "secondary_topic": "ease_of_use",
            "polarity": "positive",
            "evidence_span": "Easy to use",
        }]}),
        parser=SemanticParser(),
        guard=SemanticGuard(taxonomy),
    )
    result = service.extract(review)
    assert result.accepted_count == 1
    # The 1-star rating does not override claim-level textual polarity.
    assert result.predictions[0].polarity == Polarity.POSITIVE


def test_exact_continuous_substring_is_case_sensitive(taxonomy):
    review = ReviewInput(review_id="r1", product_id="p1", text="Gets HOT during use.")
    payload = {"claims": [{
        "primary_topic": "safety_risk", "secondary_topic": "overheating",
        "polarity": "negative", "evidence_span": "gets hot",
    }]}
    predictions = SemanticParser().parse(review, response(payload))
    result = SemanticGuard(taxonomy).apply(review, predictions)
    assert result.rejected_count == 1
    assert result.predictions[0].quality_flags == ["evidence_not_found"]


def test_empty_claims_are_valid_for_insufficient_information(taxonomy):
    review = ReviewInput(review_id="r1", product_id="p1", text="No details supplied.")
    parsed = SemanticParser().parse(
        review, response({"claims": []})
    )
    assert SemanticGuard(taxonomy).apply(review, parsed).predictions == []


def test_parser_rejects_unstructured_or_extra_fields():
    review = ReviewInput(review_id="r1", product_id="p1", text="Works.")
    with pytest.raises(SemanticParseError):
        SemanticParser().parse(review, response("not json"))
    with pytest.raises(SemanticParseError):
        SemanticParser().parse(
            review,
            response(
                {"claims": [{
                    "primary_topic": "function_performance", "secondary_topic": "charging_success",
                    "polarity": "positive", "evidence_span": "Works", "confidence": 0.99,
                }]}
            ),
        )
