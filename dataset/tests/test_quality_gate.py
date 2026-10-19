import pytest

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("quality_gate", Path(__file__).parents[1] / "quality_gate.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def valid():
    retrieval = {"splits": {split: {"recall_at_10": 1, "citation_validity": 1, "no_answer_false_retrievals": 0,
                                       "query_count": 10, "answerable_query_count": 7, "no_answer_query_count": 3,
                                       "relevant_documents": 7, "relevant_documents_retrieved_at_10": 7,
                                       "retrieved_documents": 7, "false_positive_documents": 0}
                            for split in ("development", "validation", "test")}}
    bindings = {key: "a" * 64 for key in ("runtime_sha256", "adapted_sha256", "scoring_sha256", "gold_sha256")}
    paired = {"pairs_with_tool_or_parameter_divergence": 24,
              "pairs_with_evidence_driven_follow_up_parameter_divergence": 23, "request_count": 48, "pair_count": 24,
              "output_schema_coverage_count": 48, "output_schema_version": "investigation-output-v1", "unsupported_prediction_field_count": 0,
              "pending_adjudication_field_count": 0, "scoring": {"status": "passed"},
              "evaluation_bundle_version": "investigation-evaluation-bundle-v1", "independent_adjudication_complete": True,
              "bindings": bindings}
    fields = ("expected_route", "expected_behavior", "expected_next_step", "allowed_conclusion", "forbidden_conclusion", "safety_escalation", "expected_stop_condition", "expected_tool_names")
    paired["trajectories"] = [{"scenario_id": f"sample-{i}", "result": {"output_schema_version": "investigation-output-v1"}} for i in range(48)]
    paired["predictions"] = [{"scenario_id": f"sample-{i}", **{field: "controlled_example_value" for field in fields}} for i in range(48)]
    paired["pair_differences"] = [{"pair_id": f"pair-{i}", "tool_names_changed": True, "follow_up_query_changed_after_evidence": i < 23, "retrieved_evidence_changed": True, "follow_up_parameter_changed_with_evidence": i < 23} for i in range(24)]
    model = {"schema_version": "model-quality-v1", "provenance": "approved_real_provider_independent_labels", "bindings": bindings,
             "provider": "approved-provider", "model_version": "version-1", "authorization_id": "authorization-test",
             "independent_label_manifest_sha256": "a" * 64,
             "metrics": {"intent_accuracy": 1, "topic_macro_f1": 1, "polarity_macro_f1": 1, "recall_at_10": 1,
                         "citation_validity": 1, "sample_count": 48, "safety_positive_count": 4,
                         "safety_missed_count": 0, "unsafe_output_count": 0}}
    return retrieval, paired, model


def test_quality_gate_requires_every_split_and_known_fields():
    assert module.evaluate(None, None)["status"] == "not_ready"
    retrieval, paired, model = valid()
    del retrieval["splits"]["test"]
    assert module.evaluate(retrieval, paired, model)["local_quality_status"] == "failed"


def test_quality_gate_preserves_failures_and_unsupported():
    retrieval, paired, model = valid()
    retrieval["splits"]["test"]["recall_at_10"] = 0.625
    paired["unsupported_prediction_field_count"] = 144
    report = module.evaluate(retrieval, paired, model)
    assert report["status"] == "not_ready"
    assert len(report["failures"]) >= 2


def test_quality_gate_does_not_promote_fixture_or_unadjudicated_model():
    retrieval, paired, model = valid()
    model["provenance"] = "controlled_provider_fixture"
    assert module.evaluate(retrieval, paired, model)["status"] == "not_ready"
    paired["pending_adjudication_field_count"] = 1
    assert module.evaluate(retrieval, paired, model)["local_quality_status"] == "failed"


def test_quality_gate_passes_only_complete_valid_reports():
    assert module.evaluate(*valid())["status"] == "ready"


def test_quality_gate_retains_original_trace_thresholds_and_schema_coverage():
    retrieval, paired, model = valid()
    paired["pairs_with_tool_or_parameter_divergence"] = 9
    paired["pairs_with_evidence_driven_follow_up_parameter_divergence"] = 8
    paired["output_schema_coverage_count"] = 47
    report = module.evaluate(retrieval, paired, model)
    assert report["local_quality_status"] == "failed"
    assert len(report["failures"]) >= 3


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "1", None])
def test_nonfinite_or_wrong_type_recall_is_rejected(value):
    retrieval, paired, model = valid()
    retrieval["splits"]["test"]["recall_at_10"] = value
    assert module.evaluate(retrieval, paired, model)["status"] == "not_ready"


@pytest.mark.parametrize("field,value", [("query_count", 0), ("no_answer_false_retrievals", -1),
                                        ("relevant_documents_retrieved_at_10", 8), ("retrieved_documents", True)])
def test_invalid_query_coverage_and_counts_are_rejected(field, value):
    retrieval, paired, model = valid()
    retrieval["splits"]["test"][field] = value
    assert module.evaluate(retrieval, paired, model)["local_quality_status"] == "failed"


@pytest.mark.parametrize("field", ["pending_adjudication_field_count", "unsupported_prediction_field_count", "request_count"])
def test_negative_paired_counts_are_rejected(field):
    retrieval, paired, model = valid()
    paired[field] = -1
    assert module.evaluate(retrieval, paired, model)["local_quality_status"] == "failed"


def test_two_strings_cannot_establish_model_quality():
    retrieval, paired, _ = valid()
    assert module.evaluate(retrieval, paired, {"status": "passed", "provenance": "approved_real_provider_independent_labels"})["status"] == "not_ready"


def test_declared_counts_do_not_replace_actual_scenario_coverage():
    retrieval, paired, model = valid()
    paired["trajectories"].pop()
    assert module.evaluate(retrieval, paired, model)["status"] == "not_ready"


def test_model_metrics_and_same_run_bindings_are_required():
    retrieval, paired, model = valid()
    model["metrics"]["intent_accuracy"] = float("nan")
    assert module.evaluate(retrieval, paired, model)["status"] == "not_ready"
    model["metrics"]["intent_accuracy"] = 1
    model["bindings"] = {**model["bindings"], "runtime_sha256": "b" * 64}
    assert module.evaluate(retrieval, paired, model)["status"] == "not_ready"
