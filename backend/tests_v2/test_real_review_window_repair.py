"""No paid dispatch: replay windows, diagnostic preservation and duplicate safety."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

from app.agent.planner import LLMPlanner
from app.agent.state import InvestigationState
from insight.investigation.planner import combine_text_observations, planner_evidence

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from real_review_runtime_contract import declared_date_window  # noqa: E402


@pytest.fixture
def replay():
    plan = {'date_scope': {'start': '2022-09-03', 'end': '2023-09-02'}}
    corpus = [{'business_date': '2022-09-04'}, {'business_date': '2023-07-16'}]
    scenarios = [{'query': '调查充电慢。日期范围 2022-09-03 至 2023-09-02。'}]
    return plan, corpus, scenarios


def test_index_window_uses_declared_range_not_sample_extrema(replay):
    assert declared_date_window(*replay) == ('2022-09-03', '2023-09-02')


@pytest.mark.parametrize('mode', ['missing', 'too_large', 'outside_corpus', 'different_investigation'])
def test_window_defects_fail_before_dispatch(replay, mode):
    plan, corpus, scenarios = replay
    if mode == 'missing':
        plan.clear()
    elif mode == 'too_large':
        plan['date_scope']['start'] = '2021-01-01'
    elif mode == 'outside_corpus':
        corpus[0]['business_date'] = '2022-01-01'
    else:
        scenarios[0]['query'] = '调查充电慢。日期范围 2022-09-04 至 2023-07-16。'
    with pytest.raises(ValueError):
        declared_date_window(plan, corpus, scenarios)


def observations():
    result = []
    for kind in ('reviews', 'support'):
        item = {'type': 'text_retrieval', 'query': '充电慢反馈', 'evidence_context': 'any',
            'scope': {'asin': 'PUBLIC-DEMO', 'kind': kind}, 'items': [], 'limitations': ['Unavailable evidence is not absence'],
            'match_status': 'incomplete', 'retrieval_complete': False,
            'candidate_stage': {'semantic_candidates': {'status': 'unsupported', 'error': 'embedding_index_date_coverage_mismatch',
                                                       'candidate_scores': ['audit only']}},
            'relevance_judgment': {'raw_response': 'audit only'}}
        result.append({'tool_name': 'text_retrieval', 'source_refs': [kind], 'data': planner_evidence(item)})
    return combine_text_observations(result)


def test_compacted_observations_keep_query_and_index_failure_without_raw_audit():
    obs = observations()
    assert len(obs) == 1
    assert len(obs[0]['data']['searches']) == 2
    for search in obs[0]['data']['searches']:
        assert search['query'] == '充电慢反馈'
        assert search['retrieval_diagnostics']['error'] == 'embedding_index_date_coverage_mismatch'
    assert 'candidate_scores' not in json.dumps(obs)
    assert 'raw_response' not in json.dumps(obs)


class Gateway:
    def __init__(self, query, context):
        self.query, self.context, self.requests = query, context, []

    def plan(self, **request):
        self.requests.append(json.loads(request['user']))
        return {'decision': 'call_tool', 'tool_name': 'text_evidence',
            'arguments': json.dumps({'query': self.query, 'evidence_context': self.context}), 'decision_summary': 'controlled fixture'}


def test_repeat_is_rejected_without_tool_execution_or_model_retry():
    gateway = Gateway('充电慢反馈', 'any')
    state = InvestigationState(request_id='fixture', user_query='Investigate', observations=observations())
    with pytest.raises(ValueError, match='duplicate text evidence request'):
        LLMPlanner(gateway).decide(state, ('text_evidence',))
    assert len(gateway.requests) == 1
    assert gateway.requests[0]['prior_text_requests'] == [{'query': '充电慢反馈', 'evidence_context': 'any'}]


@pytest.mark.parametrize('query,context', [('反证：正常充电', 'asserted'), ('充电慢反馈', 'negated')])
def test_new_evidence_question_or_context_remains_available(query, context):
    gateway = Gateway(query, context)
    state = InvestigationState(request_id='fixture', user_query='Investigate', observations=observations())
    result = LLMPlanner(gateway).decide(state, ('text_evidence',))
    assert result.arguments['query'] == query and result.arguments['evidence_context'] == context
    assert len(gateway.requests) == 1


def test_real_index_rejects_old_extrema_but_accepts_declared_coverage(tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace

    from insight.semantic.vector_candidates import index_path, vector_candidates

    monkeypatch.setenv('EVIDENCE_VECTOR_INDEX_DIR', str(tmp_path))
    scope = {'shop': 'fixture', 'asin': 'PUBLIC-DEMO', 'product': None, 'kind': 'support', 'source_manifest_sha256': 'fixture'}
    metadata = {'scope': scope, 'provider': {'name': 'controlled'}, 'version': 'local-qwen3-candidates-v1',
                'start': '2022-09-04', 'end': '2023-07-16'}
    path = index_path(scope)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE metadata (value TEXT NOT NULL)')
        db.execute('CREATE TABLE vectors (fact_id TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL, vector BLOB NOT NULL)')
        db.execute('INSERT INTO metadata VALUES (?)', (json.dumps(metadata),))
    rows = SimpleNamespace(count=lambda: 0, values_list=lambda *a, **k: [], filter=lambda **k: SimpleNamespace(iterator=lambda: []))
    provider = SimpleNamespace(identity=metadata['provider'])
    with pytest.raises(ValueError, match='date_coverage_mismatch'):
        vector_candidates(rows, scope, query='fixture', start=date(2022, 9, 3), end=date(2023, 9, 2), provider=provider)
    metadata.update(start='2022-09-03', end='2023-09-02')
    with sqlite3.connect(path) as db:
        db.execute('UPDATE metadata SET value=?', (json.dumps(metadata),))
    _, receipt = vector_candidates(rows, scope, query='fixture', start=date(2022, 9, 3), end=date(2023, 9, 2), provider=provider)
    assert receipt['status'] == 'ready'


def test_original_paid_wire_failure_replayed_without_network():
    """Use sealed real provider output, preserving old request omissions as evidence."""
    from quality_round_guard import file_sha
    root = Path(__file__).resolve().parents[2] / '.local/acceptance-artifacts/internal-demo-real-quality-20261013-segments-v4/paid'
    if not root.exists():
        pytest.skip('original local paid receipts unavailable')
    seal = json.loads((root / 'seal.json').read_text())
    for name in ('request-0031.json', 'request-0032.json', 'response-0032.json'):
        assert file_sha(root / name) == seal[name]
    first = json.loads(json.loads((root / 'request-0031.json').read_text())['input'][1]['content'])
    old = json.loads(json.loads((root / 'request-0032.json').read_text())['input'][1]['content'])
    raw = json.loads((root / 'response-0032.json').read_text())
    decision = json.loads(next(c['text'] for o in raw['output'] for c in o.get('content', []) if c.get('text')))
    obs = old['observations']
    assert 'prior_text_requests' not in old and 'query' not in obs[0]['data']['searches'][0]
    # Executed tool receipt supplies query and diagnostic missing from original prompt.
    obs[0]['data']['searches'] = [dict(s, query='充电慢 充电速度 充电时间 买家反馈 评论',
        retrieval_diagnostics={'status': 'unsupported', 'error': 'embedding_index_date_coverage_mismatch'})
        for s in obs[0]['data']['searches']]
    class RecordedGateway:
        calls = 0
        def plan(self, **request):
            self.calls += 1
            prompt = json.loads(request['user'])
            assert prompt['prior_text_requests']
            assert 'embedding_index_date_coverage_mismatch' in request['user']
            return decision
    gateway = RecordedGateway()
    state = InvestigationState(request_id='original-v4-replay', user_query=first['query'], observations=obs)
    with pytest.raises(ValueError, match='duplicate text evidence request'):
        LLMPlanner(gateway).decide(state, ('text_evidence',))
    assert gateway.calls == 1
