from types import SimpleNamespace

from insight.semantic.service import PROMPT_VERSION, Claim, ProviderAdapter


def test_v2_gateway_uses_frozen_prompt_30_second_timeout_and_one_retry(monkeypatch):
    from app.semantic import factory
    captured=[]
    def gateway(*, config):
        captured.append(config)
        return SimpleNamespace(config=config)
    monkeypatch.setattr(factory,"DeepSeekResponsesGateway",gateway)
    monkeypatch.setattr(factory,"LMStudioChatGateway",gateway)
    monkeypatch.setattr(factory,"build_semantic_service",lambda path,*,gateway:SimpleNamespace(gateway=gateway))
    for provider in ["deepseek","qwen"]:
        adapter=ProviderAdapter(provider)
        config=adapter.service.gateway.config
        assert config.timeout_seconds == 30 and config.max_attempts == 2
        assert config.prompt_version == PROMPT_VERSION


def test_gateway_timeout_retries_once_without_real_network():
    import pytest

    from app.semantic.config import PRIMARY_SEMANTIC_CONFIG
    from app.semantic.model_gateway import DeepSeekResponsesGateway, ModelGatewayError
    attempts=[]
    def timeout():
        attempts.append(1)
        raise TimeoutError("synthetic timeout")
    gateway=DeepSeekResponsesGateway(config=PRIMARY_SEMANTIC_CONFIG.model_copy(update={"max_attempts":2,"timeout_seconds":30}),client=object(),sleep=lambda _:None)
    with pytest.raises(ModelGatewayError) as failure:
        gateway._run_with_retry(timeout)
    assert len(attempts) == failure.value.attempts == 2


def test_adapter_keeps_guard_rejections_for_coverage_and_trace():
    adapter=ProviderAdapter.__new__(ProviderAdapter)
    adapter._review_input=lambda **kwargs:kwargs
    adapter.service=SimpleNamespace(extract=lambda review:SimpleNamespace(predictions=[SimpleNamespace(
        primary_topic="invalid",secondary_topic="invalid",polarity="negative",evidence_span="invented",
        prediction_status="rejected",quality_flags=["evidence_not_found"],raw_response_ref="response-ref",
    )]))
    claims=adapter.extract("actual review")
    assert claims == [Claim("invalid","invalid","negative","invented","rejected",("evidence_not_found",),"response-ref")]
