"""Expanded wire ceilings remain explicit, cost-bound and independent of approvals."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from prepare_real_review_quality_round import configure_planner_budget  # noqa: E402

from insight.investigation.model_limits import planner_request_limits  # noqa: E402


@pytest.fixture
def template():
    return {'environment': {'INVESTIGATION_MAX_REQUEST_BYTES': '24000'}, 'budget': {
        'input_usd_per_million': '.30', 'output_usd_per_million': '1.20', 'investigation_call_units': 12,
        'max_calls': 906, 'max_retries': 0, 'stop_on_provider_or_contract_failure': True,
        'past_approved_balance_reused': False, 'total_upper_usd': '10.07',
        'calls': {'planner': {'request_bytes': 24000, 'output_tokens': 3000, 'upper_usd': '.0108'},
                  'relevance': {'request_bytes': 24000, 'output_tokens': 6000, 'upper_usd': '.0147072'},
                  'classification': {'request_bytes': 8000, 'output_tokens': 1200, 'upper_usd': '.00384'}}}}


def enable(monkeypatch, cap, cost):
    for key, value in {'INSIGHT_ENABLED_MODEL_PROVIDERS': 'deepseek', 'INVESTIGATION_APPROVAL_REFERENCE': 'local-fixture',
        'INVESTIGATION_REGION': 'local', 'INVESTIGATION_COST_CURRENCY': 'USD',
        'INVESTIGATION_MAX_REQUEST_BYTES': str(cap), 'INVESTIGATION_MAX_OUTPUT_TOKENS': '3000',
        'INVESTIGATION_INPUT_COST_PER_MILLION': '.30', 'INVESTIGATION_OUTPUT_COST_PER_MILLION': '1.20',
        'INVESTIGATION_MAX_CALL_COST': cost, 'INVESTIGATION_MODEL': 'deepseek-flash',
        'INVESTIGATION_REASONING_EFFORT': 'none'}.items():
        monkeypatch.setenv(key, value)


def test_explicit_large_wire_and_matching_cost_allowed(monkeypatch):
    enable(monkeypatch, 65536, '.0232608')
    result = planner_request_limits('system', '"' * 20000, {'type': 'string'})
    assert result['max_request_bytes'] == 65536
    assert result['reserved_upper_cost'] == '0.0232608'


def test_raising_bytes_without_raising_cost_rejected(monkeypatch):
    enable(monkeypatch, 65536, '.0108')
    with pytest.raises(ValueError, match='approved call cost'):
        planner_request_limits('', 'small', {'type': 'string'})


def test_oversized_payload_still_rejected(monkeypatch):
    enable(monkeypatch, 65536, '.0232608')
    with pytest.raises(ValueError, match='approved byte bound'):
        planner_request_limits('', '"' * 40000, {'type': 'string'})


@pytest.mark.parametrize('cap', [0, -1, 65537])
def test_limit_outside_supported_range_rejected(monkeypatch, cap):
    enable(monkeypatch, cap, '.1')
    with pytest.raises(ValueError, match='invalid planner request bounds'):
        planner_request_limits('', '', {'type': 'string'})


def test_candidate_budget_recalculates_entire_round(template):
    old = copy.deepcopy(template)
    configure_planner_budget(template, 65536)
    budget = template['budget']
    assert budget['calls']['planner']['upper_usd'] == '0.0232608'
    assert budget['per_investigation_upper_usd'] == '0.2791296'
    assert budget['calculated_upper_usd'] == '14.9914368'
    assert budget['total_upper_usd'] == '15.00'
    assert template['environment']['INVESTIGATION_MAX_EVAL_COST'] == '15.00'
    assert budget['calls']['relevance'] == old['budget']['calls']['relevance']
    assert budget['calls']['classification'] == old['budget']['calls']['classification']
    assert not budget['past_approved_balance_reused'] and budget['max_calls'] == 906
    assert 'approval' not in template


def test_same_24k_limit_keeps_previous_envelope(template):
    configure_planner_budget(template, 24000)
    assert template['budget']['total_upper_usd'] == '10.07'


@pytest.mark.parametrize('cap', [0, 65537])
def test_candidate_preparation_rejects_invalid_limit(template, cap):
    before = copy.deepcopy(template)
    with pytest.raises(ValueError):
        configure_planner_budget(template, cap)
    assert template == before
