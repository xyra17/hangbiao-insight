import copy
import importlib.util
from pathlib import Path

import pytest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[2] / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = load('execution_audit', 'scripts/verify_quality_execution_binding.py')
contract = load('execution_contract', 'backend/insight/investigation/execution_config.py')


def reports(requested='deterministic', decision=None, judgment=None):
    config = {**contract.configuration_snapshot(requested), 'relevance_enabled': judgment is not None}
    traces = [{'decisions': [decision or {'planner': 'deterministic-investigation-v10', 'execution_origin': 'local_rule'}]}]
    queries = {'query': {'algorithm_manifest': {'relevance_enabled': config['relevance_enabled']}, 'relevance_judgment': judgment or {}}}
    binding = contract.bind_observed_execution(config, traces, queries)
    identity = contract.execution_identity(config, traces, queries, 'a' * 64)
    runtime = {'execution_configuration': binding, 'execution_identity': identity, 'runtime_corpus_manifest_sha256': 'a' * 64, 'trajectories': traces}
    retrieval = {'execution_configuration': copy.deepcopy(binding), 'execution_identity': copy.deepcopy(identity), 'runtime_corpus_manifest_sha256': 'a' * 64, 'per_query': queries}
    return runtime, retrieval


def test_legacy_requested_provider_cannot_prove_dispatch():
    result = module.audit({'planner_provider': 'deepseek'}, {'per_query': {}})
    assert result['status'] == 'failed' and not result['quality_accepted']


def test_normal_fallback_and_controlled_gateway_are_not_real_provider():
    runtime, retrieval = reports()
    assert module.audit(runtime, retrieval)['status'] == 'passed'
    runtime, retrieval = reports(judgment={'provider': 'controlled-gateway', 'execution_origin': 'controlled_or_unknown', 'dispatch_attempted': True, 'response_received': True})
    result = module.audit(runtime, retrieval)
    assert result['status'] == 'passed' and not result['recomputed_execution_configuration']['real_provider_run']


def test_requested_model_but_only_fallback_rejects_claimed_matched_and_fabricated_counts():
    runtime, retrieval = reports(requested='deepseek')
    for report in [runtime, retrieval]:
        report['execution_configuration'].update(configuration_execution_status='matched', errors=[], real_provider_run=True, external_planner_decision_count=99)
    result = module.audit(runtime, retrieval)
    assert result['status'] == 'failed'
    assert 'requested_model_without_observed_provider_execution' in result['errors']


@pytest.mark.parametrize('field,value', [('real_provider_run', True), ('external_planner_decision_count', 99), ('external_relevance_dispatch_count', 99), ('external_relevance_response_count', 99)])
def test_claimed_flags_and_counts_are_recomputed(field, value):
    runtime, retrieval = reports()
    for report in [runtime, retrieval]:
        report['execution_configuration'][field] = value
    assert module.audit(runtime, retrieval)['status'] == 'failed'


def test_known_provider_name_with_controlled_origin_does_not_establish_real_model():
    runtime, retrieval = reports(requested='deepseek', decision={'planner': 'deepseek-v4-flash-agent-planner-v2', 'execution_origin': 'controlled_or_unknown'})
    assert module.audit(runtime, retrieval)['status'] == 'failed'
    assert not module.audit(runtime, retrieval)['recomputed_execution_configuration']['real_provider_run']


@pytest.mark.parametrize('dispatch,response,expected', [(False, False, False), (True, False, False), (True, True, True), (False, True, False)])
def test_dispatch_and_response_are_distinct_observations(dispatch, response, expected):
    runtime, retrieval = reports(judgment={'provider': 'deepseek-v4-flash-relevance-v1', 'execution_origin': 'provider_response', 'dispatch_attempted': dispatch, 'response_received': response})
    result = module.audit(runtime, retrieval)
    assert result['recomputed_execution_configuration']['real_provider_run'] is expected
    assert result['status'] == ('failed' if response and not dispatch else 'passed')


@pytest.mark.parametrize('mutation', ['missing_identity', 'another_run', 'altered_trajectory', 'altered_queries', 'altered_input', 'altered_config'])
def test_same_configuration_does_not_prove_same_run(mutation):
    runtime, retrieval = reports()
    if mutation == 'missing_identity':
        runtime.pop('execution_identity')
        retrieval.pop('execution_identity')
    elif mutation == 'another_run':
        retrieval['execution_identity']['run_id'] = '00000000-0000-0000-0000-000000000000'
    elif mutation == 'altered_trajectory':
        runtime['trajectories'][0]['new_fact'] = 'changed'
    elif mutation == 'altered_queries':
        retrieval['per_query']['query']['new_fact'] = 'changed'
    elif mutation == 'altered_input':
        retrieval['runtime_corpus_manifest_sha256'] = 'b' * 64
    else:
        for report in [runtime, retrieval]:
            report['execution_configuration']['environment_default_planner'] = 'deepseek'
    assert module.audit(runtime, retrieval)['status'] == 'failed'
