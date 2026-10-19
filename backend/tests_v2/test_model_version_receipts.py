"""Configurable API alias and response-model binding, with no vendor traffic."""
from types import SimpleNamespace

import pytest

from insight.investigation.execution_config import bind_observed_execution, configuration_snapshot


def receipt(model='deepseek-flash', reported='deepseek-flash', response_id='controlled-receipt'):
    return {'provider': 'deepseek', 'requested_model': model, 'reported_model': reported, 'provider_response_id': response_id, 'version_pinned': False, 'reasoning_effort': 'low', 'max_output_tokens': 3000}


@pytest.mark.parametrize('model,reported,response_id,expected', [('deepseek-flash', 'deepseek-flash', 'id', True), ('wrong', 'deepseek-flash', 'id', False), ('deepseek-flash', None, 'id', False), ('deepseek-flash', 'deepseek-flash', None, False)])
def test_new_planner_requires_bound_requested_and_returned_model(monkeypatch, model, reported, response_id, expected):
    monkeypatch.setenv('INVESTIGATION_MODEL', 'deepseek-flash')
    monkeypatch.setenv('EVIDENCE_RELEVANCE_ENABLED', '0')
    config = configuration_snapshot('deepseek')
    result = bind_observed_execution(config, [{'decisions': [{'planner': 'deepseek-agent-planner-v3', 'execution_origin': 'provider_response', 'model_receipt': receipt(model, reported, response_id)}]}], {'one': {'algorithm_manifest': {'relevance_enabled': False}}})
    assert result['real_provider_run'] is expected


def test_relevance_response_receipt_and_cache(monkeypatch):
    monkeypatch.setenv('EVIDENCE_RELEVANCE_MODEL', 'deepseek-flash')
    monkeypatch.setenv('EVIDENCE_RELEVANCE_ENABLED', '1')
    config = configuration_snapshot('deterministic')
    row = {'algorithm_manifest': {'relevance_enabled': True}, 'relevance_judgment': {'provider': 'deepseek-relevance-v2', 'execution_origin': 'provider_response', 'dispatch_attempted': True, 'response_received': True, 'model_receipt': receipt()}}
    result = bind_observed_execution(config, [], {'one': row})
    assert result['external_relevance_response_count'] == 1
    row['relevance_judgment']['cache_reused'] = True
    assert bind_observed_execution(config, [], {'one': row})['external_relevance_response_count'] == 0


def test_factory_uses_configured_model_and_records_response(monkeypatch):
    import openai

    from app.semantic.factory import build_agent_planner
    calls = []
    response = SimpleNamespace(status='completed', output_text='{}', model='provider-returned-alias', id='controlled-id', usage=None)
    class Client:
        def __init__(self, **kwargs):
            self.responses = SimpleNamespace(create=lambda **kw: calls.append(kw) or response)
    monkeypatch.setattr(openai, 'OpenAI', Client)
    for key, val in {'DEEPSEEK_API_KEY': 'controlled', 'INVESTIGATION_MODEL': 'configured-flash', 'INSIGHT_ENABLED_MODEL_PROVIDERS': 'deepseek', 'INVESTIGATION_APPROVAL_REFERENCE': 'test-only', 'INVESTIGATION_REGION': 'test', 'INVESTIGATION_INPUT_COST_PER_MILLION': '.3', 'INVESTIGATION_OUTPUT_COST_PER_MILLION': '1.2', 'INVESTIGATION_COST_CURRENCY': 'TEST', 'INVESTIGATION_MAX_CALL_COST': '.1'}.items():
        monkeypatch.setenv(key, val)
    gateway = build_agent_planner().gateway
    gateway.plan(system='test', user='test', schema={})
    assert calls[0]['model'] == 'configured-flash'
    assert gateway.last_model_receipt['reported_model'] == 'provider-returned-alias'
    assert gateway.provenance == 'deepseek-agent-planner-v3' and gateway.last_model_receipt['version_pinned'] is False


def test_semantic_factory_keeps_historical_config_but_selects_current_alias(monkeypatch):
    import app.semantic.factory as factory
    from app.semantic.config import PRIMARY_SEMANTIC_CONFIG
    monkeypatch.setattr(factory, 'DeepSeekResponsesGateway', lambda **kwargs: kwargs['config'])
    monkeypatch.delenv('SEMANTIC_MODEL', raising=False)
    current = factory.build_semantic_gateway('deepseek')
    assert current.model == 'deepseek-flash' and PRIMARY_SEMANTIC_CONFIG.model == 'deepseek-v4-flash'
    monkeypatch.setenv('SEMANTIC_MODEL', 'approved-model')
    assert factory.build_semantic_gateway('deepseek').model == 'approved-model'


def test_silver_sdk_is_capped_and_provider_specific(monkeypatch):
    import importlib.util
    from pathlib import Path

    import openai
    spec = importlib.util.spec_from_file_location('silver_runtime', Path(__file__).resolve().parents[2] / 'scripts/ai_silver_label.py')
    silver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(silver)
    calls = []
    clients = []
    class Client:
        def __init__(self, **kwargs):
            clients.append(kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: calls.append(kw) or SimpleNamespace(model_dump=lambda **_: {'model': 'controlled'})))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(openai, 'OpenAI', Client)
    monkeypatch.setenv('OPENAI_API_KEY', 'controlled')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'controlled')
    for provider in ['openai', 'deepseek']:
        silver.sdk_call({'provider': provider, 'model': 'controlled'}, silver.messages({'text': 'review'}), {'max_output_tokens': 700})
    assert all(c['max_retries'] == 0 and c['timeout'] == 30 for c in clients)
    assert all(c['max_tokens'] == 700 for c in calls)
    assert calls[0]['response_format']['json_schema']['strict'] and calls[0]['store'] is False
    assert calls[1]['extra_body']['thinking']['type'] == 'disabled'
    assert calls[1]['response_format'] == {'type': 'json_object'}
