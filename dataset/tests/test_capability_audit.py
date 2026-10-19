import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_investigation_capabilities import audit  # noqa: E402


def test_execution_and_evidence_action_difference_are_not_semantic_correctness():
    report = {"run_terminal_success_count": 2, "pairs_with_tool_or_parameter_divergence": 0,
              "pairs_with_evidence_driven_follow_up_parameter_divergence": 0,
              "trajectories": [
                  {"scenario_id": "a", "result": {"business_contract": {"expected_next_step": "verify_original_record_with_source_owner"}},
                   "retrievals": [{"record_ids": ["original-1"]}]},
                  {"scenario_id": "b", "result": {"business_contract": {"expected_next_step": "request_matching_original_records"}},
                   "retrievals": [{"record_ids": []}]}],
              "scoring": {"pairs": [{"pair_id": "development", "variants": [{"scenario_id": "a"}, {"scenario_id": "b"}]}]}}
    result = audit(report)
    assert result["terminal_success"] == 2
    assert result["different_scoped_evidence_pairs"] == result["different_known_next_step_pairs"] == 1
    assert result["tool_or_parameter_difference_pairs"] == 0
    assert result["semantic_correctness"] == "not_established"
