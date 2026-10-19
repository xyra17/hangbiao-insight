import pytest

from insight.investigation.citations import metric_catalog
from insight.investigation.fact_followup import structured_followup
from insight.investigation.planner import DeterministicPlanner

CASES = [
    ({"daily": {"baseline": {"currency": "USD"}, "current": {"currency": "EUR"}}}, "reconcile_amounts_by_currency"),
    ({"occurrence": {"refunds": {"current": {"amount_by_currency": {"USD": "10", "EUR": "2"}}}}}, "reconcile_amounts_by_currency"),
    ({"cohort": {"refunds": {"refund_chains": [{"chain_status": "reversal_without_positive_anchor", "net_amount": "-2", "currency": "USD"}]}}}, "reconcile_order_refund_reversal_links"),
    ({"cohort": {"refunds": {"unmatched_records": 2}}}, "reconcile_order_refund_reversal_links"),
    ({"cohort": {"refunds": {"refund_chains": [{"classification": "before_shipment", "net_amount": "-10", "currency": "USD"}]}}}, "reconcile_order_refund_reversal_links"),
    ({"cohort": {"refunds": {"current": {"unverified_first_shipment_orders": 2}}}}, "restore_first_shipment_coverage"),
    ({"cohort": {"refunds": {"current": {"denominator": 0, "immature_orders": 10}, "observing_orders": 10}}}, "wait_for_complete_observation_window"),
    ({"cohort": {"returns": {"observation": {"status": "low_sample", "mature_orders": 5}}}}, "review_cases_without_population_inference"),
    ({"cohort": {"returns": {"status": "missing"}}}, "restore_cohort_source_coverage"),
    ({"occurrence": {"returns": {"baseline": {"event_basis": "requested"}, "current": {"event_basis": "received"}}}}, "separate_requested_and_received_return_sources"),
    ({"occurrence": {"returns": {"current": {"event_basis": "requested"}}}}, "verify_return_receipt_before_received_claim"),
    ({"comparability": {"status": "limited", "structure_changes": [{"product_id": "sku-1", "parent_asins": ["old", "new"]}]}}, "reconcile_product_mapping_before_comparison"),
    ({"comparability": {"status": "limited", "operations": [{"type": "promotion"}]}}, "review_overlapping_operations"),
    ({"daily": {"status": "partial", "baseline": {"complete": False}}}, "repair_daily_source_before_comparison"),
]


@pytest.mark.parametrize("metrics,next_step", CASES)
def test_structured_calculator_fact_changes_actual_business_next_step(metrics, next_step):
    evidence = {"type": "metric_snapshot", "metrics": metrics, "metric_evidence": metric_catalog(snapshot_id="s1", snapshot_fingerprint="f1",
                source_manifest={}, shop_id="shop", child_asin="B000TEST01", product_id=None, metrics=metrics)}
    result = structured_followup([evidence])
    assert result["next_step"] == next_step and result["input_paths"] and result["citations"]
    decision = DeterministicPlanner().decide(query="Check structured evidence", evidence=[evidence], history=[], available_tools=[])
    assert decision.business_contract["expected_next_step"] == next_step
    assert decision.business_contract["allowed_conclusion"] == "cited_metric_fact"
    assert decision.business_basis["stop_condition"]
    assert "因" not in decision.business_contract["allowed_conclusion"]
    # Opposite facts contain no triggered condition; no scenario IDs or gold are involved.
    assert structured_followup([{"type": "metric_snapshot", "metrics": {"daily": {"status": "ready"}}}]) is None


def test_internal_investigation_is_not_external_change_approval():
    evidence = [{"type": "authorization_boundary", "external_change_requested": True, "external_write_authorized": False}]
    assert structured_followup(evidence)["next_step"] == "request_human_approved_change_process"


def test_missing_sales_source_does_not_replace_original_record_question():
    evidence = [{"type": "metric_snapshot", "metrics": {"daily": {"status": "missing"}}},
                {"type": "text_retrieval", "match_status": "relevant_hits", "retrieval_complete": True}]
    decision = DeterministicPlanner().decide(query="请查找包装评论原文", evidence=evidence, history=[], available_tools=[])
    assert decision.business_contract["expected_next_step"] == "verify_original_record_with_source_owner"
