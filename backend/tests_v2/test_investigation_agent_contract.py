from datetime import date

import pytest

from insight.models import (
    InvestigationBudgetLedger,
    InvestigationDecision,
    InvestigationRun,
    InvestigationToolCall,
)
from insight.services import create_product
from insight.tasks import run_job

pytestmark = pytest.mark.django_db(transaction=True)


def test_fuzzy_route_returns_scoped_candidates_and_clarification_can_continue(metric_scope, client):
    users, shops = metric_scope
    create_product(
        users["admin"], shops[0],
        {"child_asin": "B000TEST02", "seller_sku": "S2", "fulfillment": "FBA", "name": "第二商品", "parent_asin": "", "owner": None, "effective_from": date(2025, 1, 1), "reason": "test"},
        "test",
    )
    client.force_authenticate(users["a"])
    response = client.post("/api/v1/investigations/", {"shop": str(shops[0].pk), "query": "请调查评论异常", "max_steps": 8, "idempotency_key": "fuzzy-1"}, format="json")
    run_job(response.data["id"])
    run = InvestigationRun.objects.get(pk=response.data["result"]["investigation_id"])
    assert run.status == "needs_clarification"
    assert len(run.result["candidates"]) == 2
    assert all(item["child_asin"] in {"B000TEST01", "B000TEST02"} for item in run.result["candidates"])
    clarified = client.post(f"/api/v1/investigations/{run.pk}/clarify/", {"query": "选第二个商品", "child_asin": "B000TEST02", "idempotency_key": "fuzzy-clarify-1"}, format="json")
    assert clarified.status_code == 202
    run_job(clarified.data["id"])
    child = InvestigationRun.objects.get(pk=clarified.data["result"]["investigation_id"])
    assert child.previous_run_id == run.pk
    assert child.child_asin == "B000TEST02"


def test_fuzzy_route_never_lists_a_product_from_another_shop(metric_scope, client):
    users, shops = metric_scope
    create_product(
        users["admin"], shops[1],
        {
            "child_asin": "B000OTHER1", "seller_sku": "OTHER-SKU", "fulfillment": "FBA",
            "name": "另一个店铺商品", "parent_asin": "", "owner": None,
            "effective_from": date(2025, 1, 1), "reason": "test",
        },
        "test",
    )
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/investigations/",
        {"shop": str(shops[0].pk), "query": "调查商品异常", "max_steps": 8, "idempotency_key": "fuzzy-scope-1"},
        format="json",
    )
    run_job(response.data["id"])
    run = InvestigationRun.objects.get(pk=response.data["result"]["investigation_id"])
    assert all(item["child_asin"] != "B000OTHER1" for item in run.result["candidates"])


def test_agent_receipts_have_budget_decisions_and_per_tool_authorization(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post("/api/v1/investigations/", {"shop": str(shops[0].pk), "query": "调查 B000TEST01 的退货评论原因", "max_steps": 8, "idempotency_key": "agent-receipt-1"}, format="json")
    run_job(response.data["id"])
    run = InvestigationRun.objects.get(pk=response.data["result"]["investigation_id"])
    assert run.status == "completed"
    assert InvestigationBudgetLedger.objects.filter(run=run, kind="model").count() >= 1
    assert InvestigationDecision.objects.filter(run=run).count() >= 1
    assert set(InvestigationToolCall.objects.filter(run=run).values_list("authorization", flat=True)) == {"allowed"}
    payload = client.get(f"/api/v1/investigations/{run.pk}/").data
    assert payload["budget"]["model_attempts"] >= 1
    assert payload["tool_calls"]
    assert {item["tool_name"] for item in payload["tool_permissions"]} == {
        "metric_snapshot",
        "review_claims",
        "text_evidence",
        "create_draft_task",
    }
    assert all(item["external_write"] is False for item in payload["tool_permissions"])


def test_deterministic_planner_changes_route_after_evidence_and_stops_on_safety():
    from insight.investigation.planner import DeterministicPlanner

    planner = DeterministicPlanner()
    first = planner.decide(
        query="分析商品异常",
        evidence=[],
        history=[],
        available_tools=["metric_snapshot", "review_claims", "text_evidence"],
    )
    assert first.next_tool == "metric_snapshot"
    review_path = planner.decide(
        query="调查评论原因",
        evidence=[{"signals": [{"metric": "sales_units"}]}],
        history=[{"tool": "metric_snapshot"}],
        available_tools=["review_claims", "text_evidence"],
    )
    assert review_path.next_tool == "review_claims"
    safety_path = planner.decide(
        query="调查评论原因",
        evidence=[{"signals": [{"metric": "safety_risk"}]}],
        history=[{"tool": "metric_snapshot"}],
        available_tools=["review_claims", "text_evidence"],
    )
    assert safety_path.next_tool == ""
    assert "安全线索" in safety_path.stop_reason
