"""Additional independent candidate-budget and failed-charge development cases."""
# ruff: noqa: F811
import json
from datetime import date

import pytest

from app.semantic.structured_response import StructuredResponseError
from insight.semantic.relevance import CONSTRAINTS, RelevanceAdapter
from tests_v2.test_semantic import review_report  # noqa: F401


class RejectGateway:
    last_usage = {"input_tokens": 20, "output_tokens": 20}

    def plan(self, **kwargs):
        request = json.loads(kwargs["user"])
        return {"request_sha256": request["request_sha256"], "decisions": [
            {"candidate_id": c["candidate_id"], "label": "irrelevant", "source_field": "text", "start": 0, "end": 0, "quote": "",
             "constraints": {k: "not_applicable" for k in CONSTRAINTS}} for c in request["candidates"]]}


@pytest.mark.parametrize("review_report", [[{"id": "DEV-PARAPHRASE", "text": "The carton arrived crushed and the protective insert was torn."}]], indirect=True)
@pytest.mark.django_db
def test_small_authorized_scope_reaches_judgment_without_lexical_match(review_report, monkeypatch, enable_workspace_model_budget):
    from insight.semantic import relevance, retrieval
    users, shops = review_report
    enable_workspace_model_budget(shops[0].workspace, users["admin"])
    monkeypatch.setenv("EVIDENCE_RELEVANCE_ENABLED", "1")
    monkeypatch.setenv("EVIDENCE_RELEVANCE_MAX_CANDIDATES", "2")
    monkeypatch.setattr(relevance, "configured_adapter", lambda: RelevanceAdapter(RejectGateway(), max_candidates=2, input_rate="1", output_rate="1"))
    result = retrieval.retrieve(users["a"], shops[0], "B000TEST01", query="packaging damage", start=date(2026, 9, 1), end=date(2026, 9, 2))
    stage = result["candidate_stage"]
    assert stage["scoped_count"] == stage["adjudication_input_count"] == 1
    assert stage["lexical_matched_count"] == 0
    assert stage["policy"] == "bounded_scope_with_constraint_adjudication"
    assert result["items"] == []  # Candidate admission never establishes relevance.


def test_planner_compaction_preserves_citations_scope_and_incomplete_search():
    from insight.investigation.citations import validate_citations
    from insight.investigation.planner import combine_text_observations, planner_evidence
    original = {"fact_id": "development", "text": "The carton was crushed.", "match_kind": "relevant_hit", "business_date": "2026-10-02"}
    scope = {"shop": "development", "asin": "BDEV000001", "start": "2026-10-01", "end": "2026-10-03"}
    request = {"status": "explicit", "start": "2026-10-01", "end": "2026-10-03"}
    observations = []
    for kind in ["reviews", "support"]:
        item = {"type": "text_retrieval", "scope": {**scope, "kind": kind}, "request_scope": request,
                "items": [original] if kind == "support" else [], "limitations": ["No causal conclusion"],
                "match_status": "hits" if kind == "support" else "incomplete", "retrieval_complete": kind == "support",
                "relevance_judgment": {"raw_response": "audit payload"}}
        observations.append({"tool_name": "text_retrieval", "status": "available", "source_refs": [kind], "data": planner_evidence(item)})
    compact = combine_text_observations(observations)
    assert len(compact) == 1
    data = compact[0]["data"]
    assert data["items"] == [original] and data["scope"] == scope and data["request_scope"] == request
    assert data["retrieval_complete"] is False and data["match_status"] == "incomplete"
    assert len(data["searches"]) == 2 and data["limitations"] == ["No causal conclusion"]
    citation = {"fact_id": "development", "quote": "carton was crushed"}
    assert validate_citations([citation], [data]) == [citation]


def test_all_planner_wire_objects_require_every_property():
    from app.agent.planner import PLANNER_SCHEMA
    from app.semantic.structured_response import validate_strict_schema
    validate_strict_schema(PLANNER_SCHEMA)
    with pytest.raises(ValueError, match="strict_schema_requires"):
        validate_strict_schema({"type": "object", "properties": {"optional": {"type": "string"}}, "required": [], "additionalProperties": False})
    with pytest.raises(ValueError, match="complex_nullable_type_requires_anyof"):
        validate_strict_schema({"type": ["object", "null"]})


def test_live_wire_schema_rejects_missing_source_field_and_extra_final_fields():
    from app.agent.output_contract import FINAL_JSON_SCHEMA
    from app.semantic.structured_response import validate_wire_value
    from insight.investigation.business_schema import BUSINESS_FIELDS, BUSINESS_SCHEMA_VERSION
    answer = {"schema_version": "agent-final-output-v4", "statement": "development",
              "citations": [{"fact_id": "development", "quote": "carton"}],
              "business_contract": {"schema_version": BUSINESS_SCHEMA_VERSION, **{field: "unknown" for field in BUSINESS_FIELDS}}}
    with pytest.raises(ValueError):
        validate_wire_value(answer, FINAL_JSON_SCHEMA)
    answer["citations"][0]["source_field"] = "text"
    validate_wire_value(answer, FINAL_JSON_SCHEMA)
    validate_wire_value(None, FINAL_JSON_SCHEMA)
    answer["confidence"] = .3
    with pytest.raises(ValueError):
        validate_wire_value(answer, FINAL_JSON_SCHEMA)


def test_provider_http_rejection_is_terminal_and_retains_error_body():
    from types import SimpleNamespace

    from app.semantic.structured_response import dispatch
    class Rejection(Exception):
        status_code = 400
        request_id = "controlled-request"
        body = {"error": {"code": "invalid_schema", "message": "controlled synthetic rejection"}}
    def create(**kwargs):
        raise Rejection()
    gateway = SimpleNamespace(last_usage={}, last_model_receipt={})
    request = {"model": "deepseek-flash", "reasoning": {"effort": "none"}, "max_output_tokens": 3000}
    with pytest.raises(StructuredResponseError) as error:
        dispatch(gateway, SimpleNamespace(responses=SimpleNamespace(create=create)), request)
    assert error.value.code == "provider_request_rejected"
    assert gateway.last_model_receipt["http_status"] == 400
    assert gateway.last_model_receipt["provider_error"] == Rejection.body
    assert not gateway.last_model_receipt["response_received"] and gateway.last_usage == {}


@pytest.mark.django_db(transaction=True)
def test_provider_rejection_leaves_partial_investigation_not_running(metric_scope, monkeypatch):
    from types import SimpleNamespace

    from insight.investigation import planner
    from insight.investigation.graph import execute_investigation
    from insight.models import InvestigationRun
    users, shops = metric_scope
    receipt = {"http_status": 400, "response_received": False, "provider_error": {"code": "controlled_rejection"}}
    class RejectedPlanner:
        planner = SimpleNamespace(gateway=SimpleNamespace(last_model_receipt=receipt, last_usage={}))

        def decide(self, **kwargs):
            raise StructuredResponseError("provider_request_rejected", receipt, {}, receipt["provider_error"])
    monkeypatch.setattr(planner, "planner_for", lambda provider=None: RejectedPlanner())
    run = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01",
        query="Independent development packaging investigation", max_steps=4, budget_units=4)
    result = execute_investigation(run)
    assert result.status == "partial" and result.stop_reason == "provider_request_rejected"
    entry = result.budget_ledger.get(kind="model")
    assert entry.metadata["model_receipt"]["http_status"] == 400
    assert entry.metadata["charge_outcome"] == "unknown_until_receipt"


@pytest.mark.django_db
def test_advertised_read_tools_preserve_operator_and_revoked_permissions(metric_scope):
    from insight.investigation.policy import permitted_read_tools
    from insight.models import InvestigationRun, ShopGrant
    users, shops = metric_scope
    run = InvestigationRun(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01")
    names = ["order_records", "metric_snapshot", "text_evidence", "unknown", "create_draft_task"]
    assert permitted_read_tools(run, names) == ["metric_snapshot", "text_evidence"]
    ShopGrant.objects.filter(user=users["a"], shop=shops[0]).delete()
    assert permitted_read_tools(run, names) == []


def test_model_planner_passes_authorized_scope_and_rejects_malformed_arguments():
    from types import SimpleNamespace

    from app.agent.planner import LLMPlanner
    from insight.investigation.planner import ModelPlanner
    captured = []
    receipt = {"response_received": True, "raw_response": {"controlled": True}}
    decision = {"decision": "call_tool", "tool_name": "text_evidence", "arguments": "{}", "decision_summary": "controlled",
                "target_hypothesis_id": None, "stop_reason": None, "final_answer": None, "confidence": None,
                "missing_fields": "", "counter_evidence_query": "counter evidence"}
    def plan(**kwargs):
        captured.append(json.loads(kwargs["user"]))
        return json.dumps(decision)
    gateway = SimpleNamespace(plan=plan, last_usage={"input_tokens": 20, "output_tokens": 20}, last_model_receipt=receipt)
    model = ModelPlanner.__new__(ModelPlanner)
    model.planner = LLMPlanner(gateway)
    context = {"shop": "authorized", "asin": "BDEV000001", "start": "2026-10-01", "end": "2026-10-03"}
    args = dict(query="development packaging", evidence=[], history=[], available_tools=["text_evidence"], request_context=context)
    assert model.decide(**args).next_tool == "text_evidence"
    assert captured[0]["observations"][0]["data"] == context
    decision["arguments"] = r'{\"query\":\"packaging\"}'
    with pytest.raises(StructuredResponseError) as invalid:
        model.decide(**args)
    assert invalid.value.code == "planner_contract_invalid"
    assert all(invalid.value.receipt[key] == value for key, value in receipt.items())
    assert "malformed tool arguments" in invalid.value.receipt["contract_error"]
    assert invalid.value.usage["input_tokens"] == 20


def test_model_request_preflight_failure_is_not_a_received_contract_failure():
    from types import SimpleNamespace

    from app.agent.planner import LLMPlanner
    from insight.investigation.planner import ModelPlanner
    def refuse(**kwargs):
        raise ValueError("planner request exceeds approved byte bound")
    model = ModelPlanner.__new__(ModelPlanner)
    model.planner = LLMPlanner(SimpleNamespace(plan=refuse, last_usage={}, last_model_receipt={}))
    with pytest.raises(StructuredResponseError) as failure:
        model.decide(query="development", evidence=[], history=[], available_tools=[])
    assert failure.value.code == "planner_request_preflight_failed"
    assert not failure.value.receipt.get("response_received")


@pytest.mark.django_db(transaction=True)
def test_tool_permission_denial_does_not_leave_running_investigation(metric_scope, monkeypatch):
    from rest_framework.exceptions import PermissionDenied

    from insight.investigation import planner
    from insight.investigation.graph import execute_investigation
    from insight.investigation.planner import PlannerDecision
    from insight.models import InvestigationRun
    users, shops = metric_scope
    class DeniedToolPlanner:
        def decide(self, **kwargs):
            return PlannerDecision(next_tool="order_records")
    monkeypatch.setattr(planner, "planner_for", lambda provider=None: DeniedToolPlanner())
    run = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01",
        query="development packaging", max_steps=4, budget_units=4)
    with pytest.raises(PermissionDenied):
        execute_investigation(run)
    run.refresh_from_db()
    assert run.status == "partial" and run.stop_reason == "investigation_permission_denied"
    assert run.tool_calls.get().authorization == "denied"


@pytest.mark.django_db(transaction=True)
def test_model_clarification_is_not_completed_and_does_not_create_task(metric_scope, monkeypatch):
    from insight.investigation import planner
    from insight.investigation.graph import execute_investigation
    from insight.investigation.planner import PlannerDecision
    from insight.models import ActionTask, InvestigationRun
    users, shops = metric_scope
    class ClarifyingPlanner:
        def decide(self, **kwargs):
            return PlannerDecision(clarification="missing business objective", stop_reason="controlled clarification")
    monkeypatch.setattr(planner, "planner_for", lambda provider=None: ClarifyingPlanner())
    run = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01",
        query="调查并创建核查任务", max_steps=4, budget_units=4, state={"write_requested": True})
    result = execute_investigation(run)
    assert result.status == "needs_clarification" and result.result["clarification"] == "missing business objective"
    assert result.tool_calls.count() == 0 and ActionTask.objects.count() == 0


@pytest.mark.parametrize("review_report", [[{"id": f"DEV-RANK-{n}", "text": "The packaging was damaged."} for n in range(5)]], indirect=True)
@pytest.mark.django_db
def test_stage_counts_distinguish_budget_unexamined_and_relevance_rejected(review_report, monkeypatch, enable_workspace_model_budget):
    from insight.semantic import relevance, retrieval
    users, shops = review_report
    enable_workspace_model_budget(shops[0].workspace, users["admin"])
    monkeypatch.setenv("EVIDENCE_RELEVANCE_ENABLED", "1")
    monkeypatch.setenv("EVIDENCE_RELEVANCE_MAX_CANDIDATES", "2")
    monkeypatch.setattr(relevance, "configured_adapter", lambda: RelevanceAdapter(RejectGateway(), max_candidates=2, input_rate="1", output_rate="1"))
    result = retrieval.retrieve(actor=users["a"], shop=shops[0], asin="B000TEST01", query="packaging damage", start=date(2026, 9, 1), end=date(2026, 9, 2))
    stage = result["candidate_stage"]
    assert stage["matched_count"] == 5 and stage["adjudication_input_count"] == 2
    assert stage["rank_or_scan_budget_unexamined_count"] == 3 and stage["relevance_rejected_count"] == 2
    assert stage["context_filtered_count"] == 0 and not result["retrieval_complete"]
    assert result["match_status"] == "incomplete"  # not a proven no-answer query


@pytest.mark.django_db(transaction=True)
def test_received_relevance_failure_records_charge_in_shared_ledger(metric_scope, enable_workspace_model_budget):
    from insight.models import InvestigationRun
    users, shops = metric_scope
    enable_workspace_model_budget(shops[0].workspace, users["admin"])
    run = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01", query="Development packaging", max_steps=4)

    class FailedGateway:
        calls = 0

        def plan(self, **kwargs):
            self.calls += 1
            raise StructuredResponseError("output_truncated", {"response_received": True, "provider_response_id": "controlled-only"},
                                          {"input_tokens": 20, "output_tokens": 20}, {"status": "incomplete"})

    gateway = FailedGateway()
    adapter = RelevanceAdapter(gateway, input_rate="1", output_rate="1")
    kwargs = {"query": "packaging damage", "candidates": [{"fact_id": "development", "text": "The packaging was damaged."}], "scope": {}, "complete": True, "budget_run": run}
    result = adapter.judge(**kwargs)
    assert result["response_received"] and result["error"] == "output_truncated" and result["cost_known"]
    assert result["selected_ids"] == []
    entry = run.budget_ledger.get(kind="relevance")
    assert entry.tokens == 40 and entry.cost > 0 and entry.used_units == 1
    assert entry.metadata["charge_outcome"] == "recorded_by_usage"
    assert adapter.judge(**kwargs)["cache_reused"] and gateway.calls == 1
