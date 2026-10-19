"""Controlled provider fixtures prove graph behavior, never model correctness."""
# ruff: noqa: F811
import json

import pytest

from app.agent.output_contract import FINAL_SCHEMA_VERSION
from app.agent.planner import LLMPlanner
from insight.investigation.business_schema import BUSINESS_FIELDS, BUSINESS_SCHEMA_VERSION
from insight.investigation.planner import ModelPlanner
from tests_v2.test_semantic import review_report  # noqa: F401


def test_frozen_scope_survives_many_observations_and_history_is_preserved():
    from types import SimpleNamespace
    captured = []
    def plan(**kwargs):
        captured.append(json.loads(kwargs["user"]))
        return {"decision": "clarify", "tool_name": None, "arguments": "{}", "decision_summary": "controlled",
                "target_hypothesis_id": None, "stop_reason": None, "final_answer": None, "confidence": None,
                "missing_fields": "business_goal", "counter_evidence_query": ""}
    model = ModelPlanner.__new__(ModelPlanner)
    model.planner = LLMPlanner(SimpleNamespace(plan=plan, last_usage={}, last_model_receipt={}))
    scope = {"shop": "authorized", "asin": "BDEV000001", "start": "2026-10-01", "end": "2026-10-03"}
    model.decide(query="development", evidence=[{"type": "metric_snapshot", "status": "missing"}] * 10,
                 history=[{"tool": "text_evidence", "hypothesis": "observed damage", "counter_evidence_query": "check denial",
                           "tool_args": {"query": "damage"}}], available_tools=[], request_context=scope)
    value = captured[0]
    assert value["frozen_scope"] == scope and value["product_id"] == scope["asin"]
    assert value["period"] == "2026-10-01..2026-10-03"
    assert value["hypotheses"][0]["summary"] == "observed damage"
    assert value["hypotheses"][0]["arguments"] == {"query": "damage"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("review_report", [[
    {"id": "D1", "text": "The packaging was damaged during delivery."},
    {"id": "D2", "text": "No packaging damage occurred during delivery."},
]], indirect=True)
@pytest.mark.parametrize("repeat", [False, True])
def test_actual_graph_allows_bounded_model_followup_and_rejects_duplicate(
    review_report, monkeypatch, repeat, enable_workspace_model_budget
):
    from insight.investigation import planner
    from insight.investigation.graph import execute_investigation
    from insight.models import InvestigationRun
    users, shops = review_report
    enable_workspace_model_budget(shops[0].workspace, users["admin"])
    for name, value in {"INVESTIGATION_PLANNER_PROVIDER": "deepseek", "INSIGHT_ENABLED_MODEL_PROVIDERS": "deepseek",
                        "INVESTIGATION_APPROVAL_REFERENCE": "controlled-no-network", "INVESTIGATION_REGION": "local",
                        "INVESTIGATION_COST_CURRENCY": "XTS", "INVESTIGATION_MAX_CALL_COST": ".01",
                        "INVESTIGATION_MAX_RUN_COST": ".16", "INVESTIGATION_INPUT_COST_PER_MILLION": ".3",
                        "INVESTIGATION_OUTPUT_COST_PER_MILLION": "1.2"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("EVIDENCE_RELEVANCE_ENABLED", "0")
    calls = []
    class FixtureGateway:
        provenance = "controlled-followup-fixture"
        execution_origin = "controlled_fixture"
        last_usage = {"input_tokens": 20, "output_tokens": 20}
        last_model_receipt = {}

        def plan(self, **kwargs):
            user = json.loads(kwargs["user"])
            calls.append(user)
            value = {"decision": "call_tool", "tool_name": "text_evidence", "arguments": json.dumps({"query": "packaging damage", "evidence_context": "any"}),
                     "decision_summary": "controlled support hypothesis", "target_hypothesis_id": None, "stop_reason": None,
                     "final_answer": None, "confidence": None, "missing_fields": "", "counter_evidence_query": "check denial"}
            if len(calls) == 2:
                originals = [item for obs in user["observations"] for item in obs["data"].get("items", [])]
                assert originals and user["frozen_scope"]["asin"] == "B000TEST01"
                assert "text_evidence" in user["available_tools"]
                value.update(arguments=json.dumps({"query": "packaging damage", "evidence_context": "any" if repeat else "negated"}),
                             decision_summary="controlled counter-evidence check")
            if len(calls) == 3:
                assert not user["available_tools"]
                value.update(decision="stop", tool_name=None, arguments="{}", stop_reason="controlled stop", final_answer={
                    "schema_version": FINAL_SCHEMA_VERSION, "statement": "Controlled fixture; no quality claim", "citations": [],
                    "business_contract": {"schema_version": BUSINESS_SCHEMA_VERSION, **{key: "unknown" for key in BUSINESS_FIELDS}}})
            return value
    model = ModelPlanner.__new__(ModelPlanner)
    model.planner = LLMPlanner(FixtureGateway())
    monkeypatch.setattr(planner, "planner_for", lambda provider=None: model)
    run = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01",
        query="Investigate packaging damage from 2026-09-01 to 2026-09-02", max_steps=8, budget_units=8,
        state={"planner_provider": "deepseek"})
    result = execute_investigation(run)
    if repeat:
        assert result.status == "partial" and result.stop_reason == "duplicate_text_evidence_request"
        assert len(calls) == 2
    else:
        assert result.status == "completed" and len(calls) == 3
        evidence = [e for e in result.result["evidence"] if e.get("type") == "text_retrieval"]
        followup = [e for e in evidence if e.get("phase") == "model_follow_up"]
        assert len(followup) == 2 and all(e["derived_from_retrieval_ids"] for e in followup)
        assert {row["record_id"] for e in followup for row in e["items"]} == {"D2"}
        assert result.state["text_search_rounds"] == 2 and result.tool_calls.count() == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("review_report", [[
    {"id": "D1", "text": "The packaging was damaged during delivery."},
    {"id": "D2", "text": "No packaging damage occurred during delivery."},
]], indirect=True)
def test_actual_graph_blocks_missing_workspace_budget_before_gateway(review_report, monkeypatch):
    from insight.investigation import planner
    from insight.investigation.graph import execute_investigation
    from insight.models import InvestigationRun

    users, shops = review_report
    for name, value in {
        "INVESTIGATION_PLANNER_PROVIDER": "deepseek",
        "INSIGHT_ENABLED_MODEL_PROVIDERS": "deepseek",
        "INVESTIGATION_APPROVAL_REFERENCE": "controlled-no-network",
        "INVESTIGATION_REGION": "local",
        "INVESTIGATION_COST_CURRENCY": "XTS",
        "INVESTIGATION_MAX_CALL_COST": ".01",
        "INVESTIGATION_MAX_RUN_COST": ".16",
        "INVESTIGATION_INPUT_COST_PER_MILLION": ".3",
        "INVESTIGATION_OUTPUT_COST_PER_MILLION": "1.2",
        "EVIDENCE_RELEVANCE_ENABLED": "0",
    }.items():
        monkeypatch.setenv(name, value)

    calls = []

    class FixtureGateway:
        provenance = "controlled-followup-fixture"
        execution_origin = "controlled_fixture"
        last_usage = {"input_tokens": 0, "output_tokens": 0}
        last_model_receipt = {}

        def plan(self, **kwargs):
            calls.append(kwargs)
            raise AssertionError("gateway must not be reached without a workspace budget")

    model = ModelPlanner.__new__(ModelPlanner)
    model.planner = LLMPlanner(FixtureGateway())
    monkeypatch.setattr(planner, "planner_for", lambda provider=None: model)
    run = InvestigationRun.objects.create(
        shop=shops[0], requested_by=users["a"], child_asin="B000TEST01",
        query="Investigate packaging damage from 2026-09-01 to 2026-09-02",
        max_steps=8, budget_units=8, state={"planner_provider": "deepseek"},
    )

    result = execute_investigation(run)

    assert result.status == "partial"
    assert not calls
    assert "workspace_model_budget_policy_missing_or_disabled" in result.stop_reason
