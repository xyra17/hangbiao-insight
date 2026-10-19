"""Actual API/Job/LangGraph/DB integration with a declared local gateway substitute."""
# ruff: noqa: F811
import json

import pytest

from app.agent.planner import LLMPlanner
from insight.investigation.planner import ModelPlanner
from tests_v2.test_semantic import review_report  # noqa: F401
from tests_v2.test_shared_business_contract import terminal


@pytest.mark.parametrize("scope_metadata", [False, True])
@pytest.mark.parametrize("protocol", ["native-v2", "native-v3"])
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('review_report', [[
    {'id': 'N1', 'text': 'The packaging was damaged during delivery.'},
    {'id': 'N2', 'text': 'No packaging damage occurred during delivery.'},
]], indirect=True)
def test_native_protocol_actual_api_job_terminal_and_blind_review(
    review_report, monkeypatch, tmp_path, protocol, scope_metadata, enable_workspace_model_budget
):
    from real_review_stage_gate import representative_gate
    from rest_framework.test import APIClient

    from insight.investigation import planner
    from insight.investigation.business_contract_registry import VERSION, review_text
    from insight.investigation.evaluation import adapt_prediction, write_template
    from tests_v2 import test_s4_s6_runtime_acceptance as helpers
    users, shops = review_report
    enable_workspace_model_budget(shops[0].workspace, users['admin'])
    for name, value in {'INVESTIGATION_PLANNER_PROTOCOL': protocol, 'INVESTIGATION_PLANNER_PROVIDER': 'deepseek',
        'INSIGHT_ENABLED_MODEL_PROVIDERS': 'deepseek', 'INVESTIGATION_APPROVAL_REFERENCE': 'controlled-no-network',
        'INVESTIGATION_REGION': 'local', 'INVESTIGATION_COST_CURRENCY': 'XTS', 'INVESTIGATION_MAX_CALL_COST': '.01',
        'INVESTIGATION_MAX_RUN_COST': '.16', 'INVESTIGATION_INPUT_COST_PER_MILLION': '.3',
        'INVESTIGATION_OUTPUT_COST_PER_MILLION': '1.2', 'EVIDENCE_RELEVANCE_ENABLED': '0'}.items():
        monkeypatch.setenv(name, value)
    calls = []
    class Gateway:
        execution_origin = 'controlled_fixture'
        provenance = 'local-native-api-integration'
        last_usage = {'input_tokens': 20, 'output_tokens': 20}
        last_model_receipt = {}
        def plan(self, **kwargs):
            data = json.loads(kwargs['user'])
            calls.append(data)
            value = terminal()
            if len(calls) < 3:
                value.update(decision='call_tool', tool_name='text_evidence', stop_reason=None, final_answer=None,
                    arguments={'query': 'packaging damage' + (' ASIN B000TEST01 2026-09-01..2026-09-02' if scope_metadata else ''), 'evidence_context': 'any' if len(calls) == 1 else 'negated'})
            else:
                originals = [i for o in data['observations'] for i in o['data'].get('items', []) if i.get('match_kind') == 'relevant_hit']
                assert originals
                first = originals[0]
                value['final_answer']['citations'] = [{'fact_id': first['fact_id'], 'quote': first['text'], 'source_field': 'text'}]
                if protocol == 'native-v3':
                    value['final_answer']['schema_version'] = 'agent-final-output-v6'
                    value['final_answer']['citations'] = [{'citation_ref': first['citation_refs']['text']}] 
            return value
    model = ModelPlanner.__new__(ModelPlanner)
    model.planner = LLMPlanner(Gateway())
    monkeypatch.setattr(planner, 'planner_for', lambda provider=None: model)
    monkeypatch.setattr(helpers, 'EVAL_PLANNER_PROVIDER', 'deepseek')
    api = APIClient()
    api.force_authenticate(users['b'])
    denied = api.post('/api/v1/investigations/', {'shop': str(shops[0].pk),
        'query': 'Inspect packaging', 'child_asin': 'B000TEST01', 'planner_provider': 'deepseek', 'idempotency_key': 'native-denied-read'}, format='json')
    assert denied.status_code in {403, 404} and not calls
    api.force_authenticate(users['admin'])
    _, trace = helpers.capture_pair_trajectory(api, users, shops[0], {
        'scenario_id': 'LOCAL-NATIVE-01', 'selected_child_asin': 'B000TEST01',
        'evidence_child_asin': 'B000TEST01', 'query': 'Investigate packaging damage from 2026-09-01 to 2026-09-02'}, 0)
    assert trace['status'] == 'completed' and trace['job']['status'] == 'completed'
    assert len(calls) == 3 and len(trace['tools']) == 2
    assert calls[1]['prior_text_requests'] and calls[2]['prior_text_requests']
    assert trace['result']['business_schema_version'] == VERSION
    assert trace['result']['business_contract']['expected_tool_names'] == ['search_feedback_evidence']
    prediction, _ = adapt_prediction(trace)
    assert prediction['expected_route'] == 'feedback_review'
    write_template(tmp_path / 'review.csv', [trace])
    assert 'feedback_review' not in (tmp_path / 'review.csv').read_text()
    assert 'feedback_review' in review_text()
    gate = representative_gate(trace, paid=False)
    assert gate['status'] == 'offline_not_real_execution_proof' and not gate['execution_gate_passed']
    # Save only when caller explicitly provides its owned evidence directory.
    import os
    from pathlib import Path
    destination = os.getenv('QUALITY_CONTRACT_LOCAL_REPORT')
    if destination:
        target = Path(destination)
        target.write_text(json.dumps({'execution_origin': 'controlled_fixture', 'external_calls': 0,
            'status': 'local_api_job_graph_passed', 'quality_accepted': False, 'trace': trace}, ensure_ascii=False, indent=2))
