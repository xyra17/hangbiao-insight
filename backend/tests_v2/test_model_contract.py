from types import SimpleNamespace

import pytest

from insight.semantic.service import PROMPT_VERSION, Claim, ProviderAdapter


def test_v2_gateway_uses_frozen_prompt_30_second_timeout_and_one_attempt(monkeypatch):
    from app.semantic import factory
    captured=[]
    def gateway(*, config):
        captured.append(config)
        return SimpleNamespace(config=config)
    monkeypatch.setattr(factory,"DeepSeekResponsesGateway",gateway)
    monkeypatch.setattr(factory,"LMStudioChatGateway",gateway)
    monkeypatch.setattr(factory,"build_semantic_service",lambda path,*,gateway:SimpleNamespace(gateway=gateway))
    monkeypatch.setenv("SEMANTIC_MAX_OUTPUT_TOKENS", "100")
    for provider in ["deepseek","qwen"]:
        adapter=ProviderAdapter(provider)
        config=adapter.service.gateway.config
        assert config.timeout_seconds == 30 and config.max_attempts == 1
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


@pytest.mark.django_db
def test_adapter_keeps_guard_rejections_for_coverage_and_trace(metric_scope, enable_workspace_model_budget, monkeypatch):
    adapter=ProviderAdapter.__new__(ProviderAdapter)
    users, shops = metric_scope
    enable_workspace_model_budget(shops[0].workspace, users["admin"])
    monkeypatch.setenv("INVESTIGATION_INPUT_COST_PER_MILLION", "1")
    monkeypatch.setenv("INVESTIGATION_OUTPUT_COST_PER_MILLION", "1")
    monkeypatch.setenv("SEMANTIC_MAX_CALL_COST", "0.03")
    monkeypatch.setenv("SEMANTIC_MAX_OUTPUT_TOKENS", "100")
    adapter._review_input=lambda **kwargs:SimpleNamespace(**kwargs)

    class StubService:
        gateway = SimpleNamespace(config=SimpleNamespace(timeout_seconds=30, max_output_tokens=100))
        prompt_builder = SimpleNamespace(build=lambda review: SimpleNamespace(system="system", user="user", response_schema={}))

        def extract(self, review, *, receipt_binding):
            self.last_response_binding = receipt_binding
            self.last_response = SimpleNamespace(
                raw_response={"id": "controlled-response", "usage": {"input_tokens": 2, "output_tokens": 2}},
                raw_response_ref="response-ref",
            )
            return SimpleNamespace(predictions=[SimpleNamespace(
                primary_topic="invalid",secondary_topic="invalid",polarity="negative",evidence_span="invented",
                prediction_status="rejected",quality_flags=["evidence_not_found"],raw_response_ref="response-ref",
            )])

    adapter.service=StubService()
    adapter.provider, adapter.model_name = "controlled", "controlled-model"
    run = SimpleNamespace(pk="run-1", shop=shops[0])
    row = SimpleNamespace(pk="review-1")
    claims=adapter.extract("actual review", run=run, source_row=row)
    assert claims == [Claim("invalid","invalid","negative","invented","rejected",("evidence_not_found",),"response-ref")]


@pytest.mark.django_db
def test_reused_provider_adapter_keeps_later_timeout_unknown_and_bound(metric_scope, enable_workspace_model_budget, monkeypatch):
    from app.semantic.runner import SemanticExtractionService
    from insight.investigation.budget import _committed_cost
    from insight.models import WorkspaceModelReservation

    users, shops = metric_scope
    enable_workspace_model_budget(shops[0].workspace, users["admin"])
    monkeypatch.setenv("INVESTIGATION_INPUT_COST_PER_MILLION", "1")
    monkeypatch.setenv("INVESTIGATION_OUTPUT_COST_PER_MILLION", "1")
    monkeypatch.setenv("SEMANTIC_MAX_CALL_COST", "0.03")
    monkeypatch.setenv("SEMANTIC_MAX_OUTPUT_TOKENS", "100")
    monkeypatch.setenv("SEMANTIC_MAX_REQUEST_BYTES", "12000")

    class Gateway:
        config = SimpleNamespace(timeout_seconds=30, max_output_tokens=100)
        attempts = 0
        network_calls = 0

        def extract(self, review, prompt):
            self.attempts += 1
            if self.attempts == 1:
                return SimpleNamespace(
                    raw_response={"id": "first-response", "usage": {"input_tokens": 11, "output_tokens": 7}},
                    raw_response_ref="first-response",
                )
            raise TimeoutError("controlled timeout after dispatch")

    class Parser:
        def parse(self, review, response):
            return []

    class Guard:
        def apply(self, review, predictions):
            return SimpleNamespace(predictions=[])

    gateway = Gateway()
    service = SemanticExtractionService(
        prompt_builder=SimpleNamespace(build=lambda review: SimpleNamespace(system="system", user="user", response_schema={})),
        gateway=gateway,
        parser=Parser(),
        guard=Guard(),
    )
    adapter = ProviderAdapter.__new__(ProviderAdapter)
    adapter._review_input = lambda **kwargs: SimpleNamespace(**kwargs)
    adapter.service = service
    adapter.provider, adapter.model_name = "controlled", "controlled-model"
    run = SimpleNamespace(pk="semantic-run-controlled", shop=shops[0])

    assert adapter.extract("first review", run=run, source_row=SimpleNamespace(pk="row-1")) == []
    with pytest.raises(TimeoutError, match="controlled timeout"):
        adapter.extract("second review", run=run, source_row=SimpleNamespace(pk="row-2"))

    first = WorkspaceModelReservation.objects.get(reservation_key__endswith="row-1")
    second = WorkspaceModelReservation.objects.get(reservation_key__endswith="row-2")
    assert first.status == "settled" and first.actual_cost is not None
    assert first.metadata["provider_response_id"] == "first-response"
    assert second.status == "unknown" and second.actual_cost is None
    assert second.metadata["usage_source"] == "none_current_attempt"
    assert second.metadata["receipt_binding"] == str(second.pk)
    assert _committed_cost(WorkspaceModelReservation.objects.filter(pk=second.pk)) == second.reserved_cost
    assert gateway.attempts == 2 and gateway.network_calls == 0
    assert service.last_response is None and service.last_response_binding is None


def test_structured_planner_accepts_evidence_driven_tool_change_and_counter_query():
    from app.agent.planner import LLMPlanner
    from app.agent.state import InvestigationState

    class Gateway:
        def __init__(self):
            self.calls = []

        def plan(self, *, system, user, schema):
            self.calls.append({"system": system, "user": user, "schema": schema})
            return {
                "decision": "call_tool",
                "tool_name": "text_evidence",
                "arguments": {"query": "找相反反馈"},
                "decision_summary": "指标与评论方向不一致，需要取反证。",
                "target_hypothesis_id": "h1",
                "stop_reason": None,
                "final_answer": None,
                "confidence": 0.72,
                "missing_fields": "",
                "counter_evidence_query": "是否存在相反极性评论？",
            }

    gateway = Gateway()
    decision = LLMPlanner(gateway).decide(
        InvestigationState(
            request_id="planner-test",
            user_query="调查退货异常",
            product_id="product-1",
            observations=[
                {"tool_name": "metric_snapshot", "status": "success", "data": {"returns": 12}, "source_refs": ["snapshot-1"]}
            ],
            hypotheses=[{"id": "h1", "summary": "退货与产品问题有关"}],
        ),
        ("review_claims", "text_evidence"),
    )
    assert decision.decision == "call_tool"
    assert decision.tool_name == "text_evidence"
    assert decision.arguments == {"query": "找相反反馈"}
    assert decision.counter_evidence_query
    assert gateway.calls[0]["schema"]["additionalProperties"] is False
