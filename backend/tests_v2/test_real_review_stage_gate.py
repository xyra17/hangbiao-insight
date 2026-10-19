"""Execution gate rejects clarification, fixture provenance and invalid citations."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from quality_round_guard import RoundGuard  # noqa: E402
from real_review_stage_gate import representative_gate  # noqa: E402


def trace():
    return {'scenario_id': 'REAL-I-01', 'status': 'completed', 'job': {'status': 'completed'},
        'requested_child_asin_scope': 'A', 'resolved_child_asin_scope': 'A',
        'decisions': [{'phase': 'after_text', 'execution_origin': 'provider_response', 'conclusion': {'statement': 'proposal'}}],
        'tools': [{'authorization': 'allowed', 'status': 'completed'}],
        'result': {'request_scope': {'start': '2022-09-03', 'end': '2023-09-02'},
            'conclusion': {'citations': [{'fact_id': 'f', 'quote': 'slow', 'source_field': 'text'}]}},
        'state_evidence': [{'type': 'text_retrieval', 'items': [{'fact_id': 'f', 'text': 'slow charging', 'match_kind': 'relevant_hit'}],
            'relevance_judgment': {'response_received': True, 'execution_origin': 'provider_response'}}]}


def test_execution_proof_is_distinct_from_quality():
    result = representative_gate(trace(), paid=True)
    assert result['execution_gate_passed'] and not result['quality_accepted']
    assert not representative_gate(trace(), paid=False)['execution_gate_passed']


@pytest.mark.parametrize('defect', ['clarification', 'no_citation', 'bad_quote', 'fixture', 'no_relevance', 'scope'])
def test_gate_rejects_incomplete_or_unbound_execution(defect):
    value = copy.deepcopy(trace())
    if defect == 'clarification':
        value['status'] = 'needs_clarification'
    elif defect == 'no_citation':
        value['result']['conclusion']['citations'] = []
    elif defect == 'bad_quote':
        value['result']['conclusion']['citations'][0]['quote'] = 'invented'
    elif defect == 'fixture':
        value['decisions'][0]['execution_origin'] = 'controlled_fixture'
    elif defect == 'no_relevance':
        value['state_evidence'][0]['relevance_judgment']['response_received'] = False
    else:
        value['result']['request_scope']['start'] = '2022-09-04'
    assert not representative_gate(value, paid=True, date_scope={'start': '2022-09-03', 'end': '2023-09-02'})['execution_gate_passed']


def test_paid_downstream_dispatch_is_locked_before_gate(tmp_path):
    from tests_v2.test_unified_quality_guard import approve, plan
    payload = plan(tmp_path)
    value = json.loads((tmp_path / 'plan.json').read_text())
    value['execution_stages'] = {'representative_id': 'REAL-I-01'}
    (tmp_path / 'plan.json').write_text(json.dumps(value))
    approve(tmp_path)
    calls = []
    guard = RoundGuard(tmp_path, paid=True, constructor=lambda **kw: calls.append(kw))
    guard.group = 'classification:x'
    with pytest.raises(RuntimeError, match='stage_locked'):
        guard.client(max_retries=0, base_url='https://api.deepseek.com').responses.create(**payload)
    assert len(guard.receipts) == 0


def test_original_v4_failure_cannot_unlock():
    path = Path(__file__).resolve().parents[2] / '.local/acceptance-artifacts/internal-demo-real-quality-20261013-segments-v4/paid/trajectory-01.json'
    if not path.exists():
        pytest.skip('original local receipt unavailable')
    result = representative_gate(json.loads(path.read_text()), paid=True)
    assert not result['execution_gate_passed']
    assert 'noncompleted_representative_investigation' in result['reasons']


def test_success_unlock_is_durable_and_trace_identity_reused_once(tmp_path):
    from tests_v2.test_unified_quality_guard import approve, plan
    plan(tmp_path)
    value = json.loads((tmp_path / 'plan.json').read_text())
    value.update(execution_stages={'representative_id': 'REAL-I-01'}, date_scope={'start': '2022-09-03', 'end': '2023-09-02'})
    (tmp_path / 'plan.json').write_text(json.dumps(value))
    approve(tmp_path)
    guard = RoundGuard(tmp_path, paid=True, constructor=None)  # no provider or network in unit check
    first = trace()
    result = guard.unlock_remaining(first)
    assert result['execution_gate_passed']
    assert guard.stage_unlocked
    assert json.loads((tmp_path / 'paid/representative-gate.json').read_text()) == result
    # A second unlock/redispatch on the same durable stage receipt is rejected.
    with pytest.raises(FileExistsError):
        guard.unlock_remaining(first)
