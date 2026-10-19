"""Approved cost envelopes with no external model dispatch."""
from types import SimpleNamespace

import pytest

from insight.investigation.model_limits import planner_request_limits, validate_cost_envelope


@pytest.fixture
def approved_limits(monkeypatch):
    for name, value in {
        'INSIGHT_ENABLED_MODEL_PROVIDERS': 'deepseek', 'INVESTIGATION_APPROVAL_REFERENCE': 'controlled-test-only',
        'INVESTIGATION_REGION': 'controlled-test-only', 'INVESTIGATION_INPUT_COST_PER_MILLION': '1',
        'INVESTIGATION_OUTPUT_COST_PER_MILLION': '2', 'INVESTIGATION_COST_CURRENCY': 'XTS',
        'INVESTIGATION_MAX_REQUEST_BYTES': '12000', 'INVESTIGATION_MAX_OUTPUT_TOKENS': '100',
        'INVESTIGATION_MAX_CALL_COST': '0.03', 'INVESTIGATION_MAX_RUN_COST': '0.24',
        'INVESTIGATION_MAX_EVAL_COST': '13', 'EVIDENCE_RELEVANCE_MAX_COST': '0.02',
    }.items():
        monkeypatch.setenv(name, value)


def test_full_evaluation_cost_bound_covers_all_calls(approved_limits):
    result = validate_cost_envelope(planner_active=True, relevance_active=True, max_steps=8, investigations=48, retrieval_queries=30)
    assert result['upper_cost'] == '12.12' and result['run_upper_cost'] == '0.24'
    assert result['limits_are_upper_bounds_not_spend']


@pytest.mark.parametrize('name,value', [('INVESTIGATION_MAX_CALL_COST', ''), ('INVESTIGATION_MAX_CALL_COST', 'NaN'),
                                      ('INVESTIGATION_MAX_RUN_COST', '0.1'), ('INVESTIGATION_MAX_EVAL_COST', '1'),
                                      ('INVESTIGATION_COST_CURRENCY', '')])
def test_missing_or_insufficient_approval_budget_blocks_before_dispatch(approved_limits, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        validate_cost_envelope(planner_active=True, relevance_active=True, max_steps=8, investigations=48, retrieval_queries=30)


def test_planner_request_is_bounded_by_bytes_output_and_quoted_cost(approved_limits, monkeypatch):
    result = planner_request_limits('system', 'query', {})
    assert result['max_output_tokens'] == 100
    assert result['reserved_upper_cost'] == '0.0122'
    with pytest.raises(ValueError):
        planner_request_limits('system', 'X' * 13000, {})
    monkeypatch.setenv('INVESTIGATION_MAX_CALL_COST', '0.001')
    with pytest.raises(ValueError):
        planner_request_limits('system', 'query', {})


def test_real_factory_passes_output_limit_to_controlled_sdk_and_refuses_over_budget(approved_limits, monkeypatch):
    import openai

    from app.semantic.factory import build_agent_planner
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status='completed', id='controlled-response', model='deepseek-flash', output_text='{}', usage=SimpleNamespace(input_tokens=2, output_tokens=2))

    monkeypatch.setenv('DEEPSEEK_API_KEY', 'controlled-not-a-secret')
    monkeypatch.setattr(openai, 'OpenAI', lambda **kwargs: SimpleNamespace(responses=SimpleNamespace(create=create)))
    gateway = build_agent_planner().gateway
    gateway.plan(system='system', user='query', schema={})
    assert len(calls) == 1 and calls[0]['max_output_tokens'] == 100
    monkeypatch.setenv('INVESTIGATION_MAX_CALL_COST', '0.001')
    with pytest.raises(ValueError):
        gateway.plan(system='system', user='query', schema={})
    assert len(calls) == 1


@pytest.mark.django_db(transaction=True)
def test_unknown_external_attempt_does_not_reuse_unit_for_second_dispatch(approved_limits, metric_scope, monkeypatch, enable_workspace_model_budget):
    from insight.investigation import graph, planner
    from insight.models import InvestigationRun
    users, shops = metric_scope
    enable_workspace_model_budget(shops[0].workspace, users["admin"])
    attempts = []

    class FailedPlanner:
        def decide(self, **kwargs):
            attempts.append(True)
            raise RuntimeError('controlled_unknown_external_outcome')

    monkeypatch.setattr(planner, 'planner_for', lambda provider=None: FailedPlanner())
    run = InvestigationRun.objects.create(shop=shops[0], requested_by=users['a'], child_asin='B000TEST01', query='Packaging evidence', max_steps=6, state={'planner_provider': 'deepseek'})
    first = graph.execute_investigation(run)
    assert first.status == 'partial' and len(attempts) == 1
    resumed = graph.execute_investigation(InvestigationRun.objects.get(pk=run.pk))
    assert resumed.status == 'partial' and len(attempts) == 1
    assert 'external_attempt_outcome_unknown' in resumed.stop_reason
    assert run.budget_ledger.get(kind='model').metadata['charge_outcome'] == 'unknown_until_receipt'


def test_missing_usage_is_unknown_not_free_even_when_rates_exist(approved_limits):
    from tests_v2.test_production_planner_protocol import run, terminal
    decision, _ = run(terminal())
    assert not decision.cost_estimate_known
