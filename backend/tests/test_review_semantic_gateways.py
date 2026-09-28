from types import SimpleNamespace

import pytest

from app.semantic.config import DEEPSEEK_FALLBACK_CONFIG, LOCAL_DEV_CONFIG, OPENAI_PRODUCTION_CONFIG, ProviderConfig
from app.semantic.model_gateway import DeepSeekResponsesGateway, LMStudioChatGateway, ModelGatewayError, OpenAIResponsesGateway
from app.semantic.prompt_builder import PromptBuilder
from app.semantic.schemas import ReviewInput
from app.semantic.taxonomy import Taxonomy


TAXONOMY = Taxonomy(
    version="taxonomy-v1.1",
    status="frozen",
    topic_structure={"function_performance": ("charging_speed",)},
)
REVIEW = ReviewInput(review_id="r1", product_id="p1", text="Very fast")
PROMPT = PromptBuilder(TAXONOMY).build(REVIEW)


class FakeResponse:
    id = "resp-1"
    output_text = '{"claims": []}'

    def model_dump(self, mode="python"):
        return {"id": self.id, "output_text": self.output_text}


def test_openai_gateway_uses_responses_reasoning_strict_schema_and_metadata():
    calls = []
    client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or FakeResponse())
    )
    response = OpenAIResponsesGateway(config=OPENAI_PRODUCTION_CONFIG, client=client).extract(REVIEW, PROMPT)
    call = calls[0]
    assert call["model"] == "gpt-5.6-sol"
    assert call["reasoning"] == {"effort": "low"}
    assert call["text"]["format"]["type"] == "json_schema"
    assert call["text"]["format"]["strict"] is True
    assert call["metadata"]["prompt_version"] == "semantic-prompt-v1.0"
    assert call["metadata"]["taxonomy_version"] == "taxonomy-v1.1"
    assert call["store"] is False
    assert response.provider == "openai"
    assert response.reasoning_effort == "low"


def test_openai_gateway_requires_environment_key_when_constructing_client(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        OpenAIResponsesGateway(config=OPENAI_PRODUCTION_CONFIG)


def test_deepseek_gateway_uses_responses_schema_and_frozen_metadata():
    calls = []
    client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or FakeResponse())
    )
    response = DeepSeekResponsesGateway(config=DEEPSEEK_FALLBACK_CONFIG, client=client).extract(
        REVIEW, PROMPT
    )
    call = calls[0]
    assert call["model"] == "deepseek-v4-flash"
    assert call["reasoning"] == {"effort": "low"}
    assert call["text"]["format"]["type"] == "json_schema"
    assert call["text"]["format"]["schema"] == PROMPT.response_schema
    assert call["store"] is False
    assert response.provider == "deepseek"
    assert response.model_name == "deepseek-v4-flash"
    assert response.reasoning_effort == "low"
    assert response.prompt_version == "semantic-prompt-v1.0"
    assert response.taxonomy_version == "taxonomy-v1.1"


def test_deepseek_gateway_requires_environment_key_when_constructing_client(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        DeepSeekResponsesGateway(config=DEEPSEEK_FALLBACK_CONFIG)


def test_deepseek_retry_is_finite():
    attempts = []

    def fail(**kwargs):
        attempts.append(kwargs)
        raise ConnectionError("offline")

    client = SimpleNamespace(responses=SimpleNamespace(create=fail))
    config = DEEPSEEK_FALLBACK_CONFIG.model_copy(update={"max_attempts": 3})
    gateway = DeepSeekResponsesGateway(config=config, client=client, sleep=lambda _: None)
    with pytest.raises(ModelGatewayError) as captured:
        gateway.extract(REVIEW, PROMPT)
    assert len(attempts) == 3
    assert captured.value.attempts == 3


def test_retry_is_finite_and_reports_attempt_count():
    attempts = []

    def fail(**kwargs):
        attempts.append(kwargs)
        raise ConnectionError("offline")

    client = SimpleNamespace(responses=SimpleNamespace(create=fail))
    config = OPENAI_PRODUCTION_CONFIG.model_copy(update={"max_attempts": 2})
    gateway = OpenAIResponsesGateway(config=config, client=client, sleep=lambda _: None)
    with pytest.raises(ModelGatewayError) as captured:
        gateway.extract(REVIEW, PROMPT)
    assert len(attempts) == 2
    assert captured.value.attempts == 2


def test_lmstudio_uses_json_schema_and_accepts_reasoning_content_fallback():
    calls = []
    message = SimpleNamespace(
        content="",
        model_extra={"reasoning_content": '{"claims": []}'},
    )
    completion = SimpleNamespace(
        id="local-1",
        choices=[SimpleNamespace(message=message)],
        model_dump=lambda mode="python": {"id": "local-1"},
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or completion)
        )
    )
    response = LMStudioChatGateway(config=LOCAL_DEV_CONFIG, client=client).extract(REVIEW, PROMPT)
    assert response.payload == '{"claims": []}'
    assert calls[0]["response_format"]["json_schema"]["strict"] is True
    assert calls[0]["reasoning_effort"] == "low"


def test_provider_config_rejects_unbounded_retry():
    with pytest.raises(ValueError):
        ProviderConfig(
            provider="openai",
            model="gpt-5.6-sol",
            api_name="responses",
            max_attempts=999,
        )
