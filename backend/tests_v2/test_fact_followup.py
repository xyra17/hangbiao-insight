from insight.investigation.evaluation import adapt_prediction
from insight.investigation.output import compose_output
from insight.investigation.planner import DeterministicPlanner


def result(evidence):
    decision = DeterministicPlanner().decide(query="Inspect business evidence", evidence=evidence, history=[], available_tools=[])
    return compose_output(evidence, [decision.as_dict()])


def test_actual_next_step_changes_with_structured_evidence_not_query_id():
    hit = result([{"type": "text_retrieval", "match_status": "relevant_hits", "retrieval_complete": True}])
    empty = result([{"type": "text_retrieval", "match_status": "no_hits", "retrieval_complete": True}])
    assert hit["business_contract"]["expected_next_step"] == "verify_original_record_with_source_owner"
    assert empty["business_contract"]["expected_next_step"] == "request_matching_original_records"
    assert hit["business_contract"]["allowed_conclusion"] == empty["business_contract"]["allowed_conclusion"] == "unknown"
    assert hit["conclusion"]["status"] == empty["conclusion"]["status"] == "unknown"
    _, metadata = adapt_prediction({"scenario_id": "independent-hit", "result": hit})
    assert metadata["field_statuses"]["expected_next_step"] == "deterministic_rule_proposal"


def test_missing_truncated_and_unready_data_have_specific_followups():
    assert result([{"type": "metric_snapshot", "status": "missing"}])["business_contract"]["expected_next_step"] == "create_current_metric_snapshot"
    assert result([{"type": "text_retrieval", "retrieval_complete": False}])["business_contract"]["expected_next_step"] == "narrow_evidence_scope_and_retry"
    for status in ("stale", "partial", "invalid"):
        evidence = [{"type": "metric_snapshot", "metrics": {"daily": {"status": status, "reason": "Missing coverage"}}}]
        assert result(evidence)["business_contract"]["expected_next_step"] == "repair_daily_source_before_comparison"
    assert result([])["business_contract"]["expected_next_step"] == "unknown"
