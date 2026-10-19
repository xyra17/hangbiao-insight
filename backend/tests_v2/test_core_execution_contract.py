"""Independent development checks; no frozen evaluation inputs or provider calls."""
import json
import os
from datetime import date
from pathlib import Path

import pytest

from insight.investigation.execution_config import bind_observed_execution, configuration_snapshot
from tests_v2.test_semantic import review_report  # noqa: F401


def test_requested_model_is_not_actual_provider_execution(monkeypatch):
    monkeypatch.setenv('INVESTIGATION_PLANNER_PROVIDER', 'deepseek')
    config = configuration_snapshot('deepseek')
    assert config['api_default_planner'] == 'deterministic'
    assert config['environment_default_planner'] == 'deepseek'
    output = bind_observed_execution(config, [{'decisions': [{'planner': 'deterministic-investigation-v10'}]}], {'one': {'algorithm_manifest': {'relevance_enabled': False}}})
    assert output['configuration_execution_status'] == 'failed' and not output['real_provider_run']
    assert output['production_configuration_approved'] is False


def test_observed_configuration_binds_model_and_relevance_without_claiming_quality():
    config = configuration_snapshot('deepseek')
    output = bind_observed_execution(config, [{'decisions': [{'planner': 'deepseek-v4-flash-agent-planner-v2', 'execution_origin': 'provider_response'}]}], {'one': {'algorithm_manifest': {'relevance_enabled': False}}})
    assert output['real_provider_run'] and output['configuration_execution_status'] == 'matched'
    assert not output['quality_or_business_accepted']


@pytest.mark.django_db
@pytest.mark.parametrize('review_report', [[
    {'id': 'PACK', 'text': 'The packaging is intact.'},
    {'id': 'DAMAGED', 'text': 'The cable was damaged.'},
    {'id': 'NEITHER', 'text': 'The delivery was prompt.'},
]], indirect=True)
def test_approved_candidate_union_is_adjudicated_not_returned_as_lexical_evidence(review_report, monkeypatch, enable_workspace_model_budget):  # noqa: F811
    from insight.semantic import relevance, retrieval
    from insight.semantic.relevance import CONSTRAINTS, RelevanceAdapter
    users, shops = review_report
    enable_workspace_model_budget(shops[0].workspace, users["admin"])
    kwargs = dict(actor=users['a'], shop=shops[0], asin='B000TEST01', query='packaging damage', start=date(2026, 9, 1), end=date(2026, 9, 2))
    before = retrieval.retrieve(**kwargs)
    assert before['candidate_stage']['matched_count'] == 0
    requests = []

    class Gateway:
        provenance = 'controlled-development-not-real-provider'
        last_usage = {'input_tokens': 20, 'output_tokens': 20}

        def plan(self, **request):
            value = json.loads(request['user'])
            requests.append(value)
            return json.dumps({'request_sha256': value['request_sha256'], 'decisions': [
                {'candidate_id': item['candidate_id'], 'label': 'irrelevant', 'source_field': 'text', 'start': 0, 'end': 0, 'quote': '',
                 'constraints': {key: 'not_applicable' for key in CONSTRAINTS}} for item in value['candidates']]})

    monkeypatch.setenv('EVIDENCE_RELEVANCE_ENABLED', '1')
    monkeypatch.setenv('EVIDENCE_RELEVANCE_MAX_CANDIDATES', '2')
    monkeypatch.setattr(relevance, 'configured_adapter', lambda: RelevanceAdapter(Gateway(), input_rate='1', output_rate='1'))
    after = retrieval.retrieve(**kwargs)
    assert after['candidate_stage']['matched_count'] == 2
    assert len(requests[0]['candidates']) == 2
    assert not after['items'] and after['relevance_judgment']['complete']
    assert after['candidate_stage']['policy'] == 'bounded_union_with_constraint_adjudication'
    save_development_trace('candidate', {'before': before, 'after': after, 'controlled_gateway_calls': requests})


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("override", [None, "shop", "date", "context_only"])
def test_evidence_driven_planner_query_reaches_actual_tool_under_frozen_scope(metric_scope, monkeypatch, override):
    from insight.investigation import graph, planner
    from insight.models import InvestigationRun
    from insight.semantic import retrieval
    users, shops = metric_scope
    observed = []

    class Planner:
        def decide(self, **kwargs):
            if not kwargs['history']:
                return planner.PlannerDecision(next_tool='metric_snapshot')
            if 'text_evidence' in kwargs['available_tools']:
                assert kwargs['evidence'][0]['type'] == 'metric_snapshot'
                args = {'query': 'packaging damage', 'evidence_context': 'negated'}
                if override == 'context_only':
                    args.pop('query')
                if override == 'shop':
                    args['shop'] = str(shops[1].pk)
                if override == 'date':
                    args['query'] = 'packaging damage since Monday'
                return planner.PlannerDecision(next_tool='text_evidence', tool_args=args)
            return planner.PlannerDecision(stop_reason='Development complete')

    monkeypatch.setattr(planner, 'planner_for', lambda provider=None: Planner())
    original = retrieval.retrieve

    def capture(*args, **kwargs):
        observed.append({k: str(kwargs[k]) for k in ['query', 'evidence_context', 'start', 'end', 'manifest']})
        return original(*args, **kwargs)

    monkeypatch.setattr(retrieval, 'retrieve', capture)
    run = InvestigationRun.objects.create(shop=shops[0], requested_by=users['a'], child_asin='B000TEST01', query='Investigate packaging evidence', max_steps=6)
    result = graph.execute_investigation(run)
    save_development_trace('planner_' + str(override), {'status': result.status, 'stop_reason': result.stop_reason, 'decisions': result.decisions, 'frozen_scope': result.input_versions.get('request_scope'), 'actual_tool_requests': observed, 'budget': {'steps_used': result.steps_used, 'remaining': result.budget_units}})
    if override in {"shop", "date"}:
        assert result.status == 'partial' and not observed
        return
    assert result.status == 'completed', result.stop_reason
    expected_query = 'Investigate packaging evidence' if override == 'context_only' else 'packaging damage'
    assert observed and all(row['query'] == expected_query for row in observed)
    assert observed[0]['evidence_context'] == 'negated'
    assert all(row['start'] == result.state['search_start'] and row['end'] == result.state['search_end'] for row in observed)
    from insight.investigation.execution_config import observed_text_parameters
    requests = [row for row in result.result['evidence'] if row.get('type') == 'text_retrieval']
    trace = {'decisions': result.decisions, 'retrievals': requests}
    assert observed_text_parameters(trace, after_evidence=True)
    save_development_trace('association_' + str(override), trace)
    from insight.investigation.execution_config import (
        bind_observed_execution,
        configuration_snapshot,
        content_digest,
        execution_identity,
    )
    config = configuration_snapshot('deterministic')
    per_query = {row['id']: {'algorithm_manifest': row['algorithm_manifest'], 'relevance_judgment': row['relevance_judgment'], 'query': row['query']} for row in requests}
    binding = bind_observed_execution(config, [trace], per_query)
    inputs = result.input_versions
    input_hash = content_digest(inputs)
    identity = execution_identity(config, [trace], per_query, input_hash)
    save_development_trace('paired_' + str(override), {'trajectories': [trace], 'execution_configuration': binding, 'execution_identity': identity, 'runtime_corpus_manifest_sha256': input_hash, 'input_manifest': inputs})
    save_development_trace('retrieval_' + str(override), {'per_query': per_query, 'execution_configuration': binding, 'execution_identity': identity, 'runtime_corpus_manifest_sha256': input_hash, 'input_manifest': inputs})



def test_model_planner_cannot_dispatch_from_allowlist_without_approval(monkeypatch):
    from insight.investigation import planner
    monkeypatch.setenv('INSIGHT_ENABLED_MODEL_PROVIDERS', 'deepseek')
    monkeypatch.delenv('INVESTIGATION_APPROVAL_REFERENCE', raising=False)
    called = []
    monkeypatch.setattr(planner, '_external_planner', lambda: called.append(True))
    with pytest.raises(planner.PlannerUnavailable):
        planner.planner_for('deepseek')
    assert not called


def save_development_trace(name, data):
    suffix = os.getenv('INVESTIGATION_EVAL_REPORT_SUFFIX')
    if not suffix:
        return
    assert suffix.replace('_', '').replace('-', '').isalnum()
    path = Path(__file__).resolve().parents[2] / '.local/acceptance-artifacts/development-core' / (name + '_' + suffix + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump({'source_nature': 'independent_synthetic_development', 'real_provider_called': False, 'quality_accepted': False, **data}, stream, ensure_ascii=False, indent=2)


def test_relevance_configuration_is_not_dispatch_and_controlled_gateway_is_not_real():
    config = {**configuration_snapshot('deterministic'), 'relevance_enabled': True}
    for provider, dispatched, expected in [('deepseek-v4-flash-relevance-v1', False, False), ('controlled-development-not-real-provider', True, False), ('deepseek-v4-flash-relevance-v1', True, True)]:
        result = bind_observed_execution(config, [], {'query': {'algorithm_manifest': {'relevance_enabled': True}, 'relevance_judgment': {'provider': provider, 'execution_origin': 'provider_response', 'dispatch_attempted': dispatched, 'response_received': dispatched}}})
        assert result['real_provider_run'] is expected


@pytest.mark.parametrize('args', [{'query': 'packaging damage'}, {'evidence_context': 'negated'}])
def test_parameter_measurement_requires_explicit_decision_receipt(args):
    from insight.investigation.execution_config import observed_text_parameters
    decision = {'decision_hash': 'after-metric', 'phase': 'after_metric', 'next_tool': 'text_evidence', 'tool_args': args}
    trace = {'decisions': [decision], 'retrievals': []}
    assert not observed_text_parameters(trace, after_evidence=True)
    # Identical text cannot associate an unlinked request or another phase.
    request = {'retrieval_id': 'initial', 'phase': 'initial', 'query': 'packaging damage', 'evidence_context': 'negated'}
    trace['retrievals'] = [request]
    assert not observed_text_parameters(trace, after_evidence=True)
    request['origin_decision_hash'] = 'after-metric'
    assert observed_text_parameters(trace, after_evidence=True) == [('initial', 'packaging damage', 'negated')]
    trace['decisions'].append({'decision_hash': 'route', 'phase': 'route', 'next_tool': 'text_evidence'})
    request['origin_decision_hash'] = 'route'
    assert not observed_text_parameters(trace, after_evidence=True)
    request['phase'] = 'counter_evidence'
    request['derived_from_retrieval_ids'] = ['not-executed']
    assert not observed_text_parameters(trace, after_evidence=True)


def test_counter_parameters_require_actual_parents_with_same_decision():
    from insight.investigation.execution_config import observed_text_parameters
    trace = {'decisions': [{'decision_hash': 'route', 'phase': 'route', 'next_tool': 'text_evidence'}],
             'retrievals': [{'retrieval_id': 'first', 'origin_decision_hash': 'route', 'phase': 'initial', 'query': 'packaging', 'evidence_context': 'any'},
                           {'retrieval_id': 'counter', 'origin_decision_hash': 'route', 'phase': 'counter_evidence', 'query': 'packaging', 'evidence_context': 'negated', 'derived_from_retrieval_ids': ['first']}]}
    assert observed_text_parameters(trace, after_evidence=True) == [('counter_evidence', 'packaging', 'negated')]
    trace['retrievals'][0]['origin_decision_hash'] = 'unrelated'
    assert not observed_text_parameters(trace, after_evidence=True)
