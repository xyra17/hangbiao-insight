import csv
import importlib.util
import json
from pathlib import Path

import pytest

from insight.investigation.evaluation import adapt_prediction, load_adjudications, write_template
from insight.investigation.graph import evidence_driven_counter_search as evidence_driven_counter_query
from insight.investigation.output import compose_output
from insight.models import InvestigationRun
from insight.semantic.safety_context import safety_context
from insight.tasks import run_job
from tests_v2.test_semantic import review_report  # noqa: F401


def test_context_is_not_confirmed_evidence_and_limitations_propagate():
    evidence = [{"type": "text_retrieval", "id": "r1", "match_status": "scope_context", "limitations": ["仅范围上下文"],
                 "items": [{"fact_id": "f1", "text": "The box is intact.", "match_kind": "scope_context"}]}]
    result = compose_output(evidence, [{"conclusion": {"statement": "Unsupported cause", "citations": [{"fact_id": "f1", "quote": "The box is intact."}]}}])
    assert result["conclusion"]["status"] == "unknown"
    assert result["conclusion"]["citation_status"] == "missing_or_invalid"
    assert result["retrieval_summary"]["context_searches"] == 1
    assert "仅范围上下文" in result["limitations"]


def test_asserted_hazard_is_not_suppressed_by_other_negated_or_hypothetical_records():
    result = evidence_driven_counter_query("核查安全反馈", [{"items": [{"text": "No overheating occurred."},
                                                                      {"text": "Could it overheat?"},
                                                                      {"text": "It overheated and emitted smoke."}]}])
    assert result["evidence_context"] == "negated"
    assert safety_context("No overheating, but smoke was observed.", "smoke") == "asserted"
    assert safety_context("No overheating, but smoke was observed.", "overheating") == "negated"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("production_gateway", [False, True])
@pytest.mark.parametrize("review_report", [[{"id": "FIXTURE", "text": "The device overheated and was disconnected."}]], indirect=True)
def test_controlled_provider_output_adjudication_and_scoring_pipeline(review_report, client, monkeypatch, tmp_path, production_gateway):  # noqa: F811
    from app.agent.investigation import AgentDecision
    from insight.investigation import planner

    class Gateway:
        last_usage = {}
        provenance = "controlled-provider-fixture-v1"

    class ProviderFixture:
        gateway = Gateway()

        def decide(self, state, available_tools):
            if "metric_snapshot" in available_tools:
                return AgentDecision(decision="call_tool", tool_name="metric_snapshot", decision_summary="Read authorized metrics")
            if "text_evidence" in available_tools:
                return AgentDecision(decision="call_tool", tool_name="text_evidence", decision_summary="Read scoped original evidence")
            hits = [hit for obs in state.observations for hit in obs["data"].get("items", []) if hit.get("match_kind") == "relevant_hit"]
            conclusion = {"statement": "Found an original record" if hits else "No relevant original evidence", "citations":
                          [{"fact_id": hits[0]["fact_id"], "quote": hits[0]["text"]}] if hits else []}
            conclusion["business_contract"] = {
                "schema_version": "investigation-business-contract-v1", "expected_route": "retrieve_reviews",
                "expected_behavior": "cite_observed_review_without_population_inference" if hits else "retain_unknown",
                "expected_next_step": "cite_the_original_record" if hits else "request_related_evidence",
                "allowed_conclusion": "original_review_reports_overheating" if hits else "no_matching_original_evidence_in_scope",
                "forbidden_conclusion": "overheating_is_population_wide" if hits else "overheating_does_not_exist",
                "safety_escalation": "urgent_handoff" if hits else "none", "expected_stop_condition": "stop_after_case_level_evidence" if hits else "stop_without_matching_evidence",
                "expected_tool_names": '["retrieve_reviews"]',
            }
            return AgentDecision(decision="stop", stop_reason="fixture_complete", final_answer=json.dumps(conclusion))

    provider = planner.ModelPlanner.__new__(planner.ModelPlanner)
    if production_gateway:
        from app.agent.output_contract import FINAL_SCHEMA_VERSION
        from app.agent.planner import LLMPlanner
        from app.agent.state import InvestigationState

        class ProductionGateway(Gateway):
            def plan(self, *, system, user, schema):
                request = json.loads(user)
                state = InvestigationState(request_id="production-fixture", user_query=request["query"], observations=request["observations"])
                decision = ProviderFixture().decide(state, tuple(request["available_tools"]))
                payload = decision.model_dump(mode="json", exclude={"action", "params", "reason"})
                if decision.final_answer:
                    payload["final_answer"] = {"schema_version": FINAL_SCHEMA_VERSION, **json.loads(decision.final_answer)}
                return json.dumps(payload)

        provider.planner = LLMPlanner(ProductionGateway())
    else:
        provider.planner = ProviderFixture()
    monkeypatch.setattr(planner, "planner_for", lambda _provider: provider)
    users, shops = review_report
    client.force_authenticate(users["a"])
    traces = []
    for n, query in enumerate(("查找过热原文", "查找紫色彗星原文")):
        response = client.post("/api/v1/investigations/", {"shop": str(shops[0].pk), "child_asin": "B000TEST01",
                               "query": query, "max_steps": 8, "idempotency_key": f"controlled-output-{n}"}, format="json")
        assert response.status_code == 202
        run_job(response.data["id"])
        run = InvestigationRun.objects.get(pk=response.data["result"]["investigation_id"])
        assert run.status == "completed"
        traces.append({"scenario_id": f"fixture-{n}", "status": run.status, "stop_reason": run.stop_reason,
                       "tools": [{"name": call.tool_name, "status": call.status} for call in run.tool_calls.order_by("sequence")],
                       "result": run.result})
    assert traces[0]["result"]["conclusion"]["citation_status"] == "verified_original_spans"
    assert traces[0]["result"]["conclusion"]["status"] == "pending_adjudication"
    assert traces[1]["result"]["conclusion"]["status"] == "unknown"
    path = tmp_path / "judgments.csv"
    write_template(path, traces)
    rows = list(csv.DictReader(path.open()))
    semantic = [("cite_observed_review_without_population_inference", "original_review_reports_overheating", "overheating_is_population_wide"),
                ("retain_unknown", "no_matching_original_evidence_in_scope", "overheating_does_not_exist")]
    for row, labels in zip(rows, semantic, strict=True):
        row.update(reviewer="controlled-fixture-reviewer", reviewed_at="2026-10-06T00:00:00Z",
                   adjudication_kind="controlled_fixture", expected_route="retrieve_reviews", expected_next_step="cite_the_original_record" if labels[0] == "cite_observed_review_without_population_inference" else "request_related_evidence",
                   expected_stop_condition="stop_after_case_level_evidence" if labels[0] == "cite_observed_review_without_population_inference" else "stop_without_matching_evidence", expected_tool_names='["retrieve_reviews"]',
                   expected_behavior=labels[0], allowed_conclusion=labels[1],
                   forbidden_conclusion=labels[2], safety_escalation="urgent_handoff" if labels[0] == "cite_observed_review_without_population_inference" else "none")
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    judgments = load_adjudications(path, traces)
    predictions = [adapt_prediction(trace, judgments[trace["scenario_id"]])[0] for trace in traces]
    gold = [{"scenario_id": f"fixture-{n}", "pair_id": "fixture-pair", "evidence_variant": str(n),
             "expected_route": "retrieve_reviews", "expected_next_step": "cite_the_original_record" if n == 0 else "request_related_evidence",
             "expected_stop_condition": "stop_after_case_level_evidence" if n == 0 else "stop_without_matching_evidence", "expected_tool_names": '["retrieve_reviews"]',
             "expected_behavior": labels[0], "allowed_conclusion": labels[1], "forbidden_conclusion": labels[2], "safety_escalation": "urgent_handoff" if n == 0 else "none"}
            for n, labels in enumerate(semantic)]
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("paired_score", root / "dataset/score_paired_investigations.py")
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    report = scorer.score_rows(gold, predictions, mode="controlled_provider_fixture")
    assert report["status"] == "passed" and report["pairs_passed"] == 1
    wrong = [dict(row) for row in predictions]
    wrong[0]["expected_route"] = "check_coverage"
    assert scorer.score_rows(gold, wrong, mode="controlled_wrong_business_route")["status"] == "failed"
    actual_proposals = [adapt_prediction(trace)[0] for trace in traces]
    assert scorer.score_rows(gold, actual_proposals, mode="actual_controlled_business_output")["status"] == "passed"
    missing = [adapt_prediction({**trace, "result": {}})[0] for trace in traces]
    assert scorer.score_rows(gold, missing, mode="missing_actual_business_output")["status"] == "not_established"
    artifact = root / (".local/third-review-fixes-20261007/controlled_production_pipeline.json" if production_gateway else ".local/third-review-fixes-20261007/controlled_legacy_pipeline.json")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"status": "passed", "provenance": "controlled_provider_fixture",
                                    "real_model_quality": "not_established", "traces": traces, "scoring": report}, ensure_ascii=False, indent=2))
