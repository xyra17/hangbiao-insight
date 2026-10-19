import copy

import pytest

from insight.investigation.query_binding import bind_text_query

SCOPE = {'shop': 'shop', 'asin': 'B07P9S6R9Y', 'product_id': 'product', 'start': '2022-09-03', 'end': '2023-09-02'}


def test_exact_original_model_metadata_binds_without_scope_change():
    original = '充电慢 买家反馈 原文 reviews ASIN B07P9S6R9Y 2022-09-03..2023-09-02'
    frozen = copy.deepcopy(SCOPE)
    query, receipt = bind_text_query(original, frozen)
    assert query == '充电慢 买家反馈 原文 reviews'
    assert receipt['original_query'] == original
    assert not receipt['scope_changed'] and not receipt['content_inferred']
    assert len(receipt['removed_exact_metadata']) == 2 and frozen == SCOPE


@pytest.mark.parametrize('suffix', ['2022-09-03至2023-09-02', '2022-09-03 to 2023-09-02', '2022-09-03..2023-09-02'])
def test_same_full_range_is_redundant_metadata(suffix):
    assert bind_text_query('slow charge ' + suffix, SCOPE)[0] == 'slow charge'


@pytest.mark.parametrize('query', ['充电慢 2022-09-04..2023-09-02', '充电慢 2021-09-03..2023-09-02',
    '充电慢 最近一个月', '充电慢 2022-09-03', '充电慢 2022-09-03..2023-09-02 排除2022-10-01',
    '充电慢 不要2022-09-03至2023-09-02', '充电慢 ASIN B000TEST01', 'ASIN B07P9S6R9Y 2022-09-03..2023-09-02'])
def test_conflicting_ambiguous_or_empty_content_rejected(query):
    with pytest.raises(ValueError):
        bind_text_query(query, SCOPE)


def test_original_quote_and_business_numbers_are_not_metadata():
    query = '寻找原文“2022-09-03..2023-09-02 ASIN B07P9S6R9Y”以及20W充电器反馈'
    assert bind_text_query(query, SCOPE) == (query, None)
    assert bind_text_query('20W charge overheats', {}) == ('20W charge overheats', None)


def test_duplicate_scope_binding_retains_same_content():
    plain, _ = bind_text_query('slow charge', SCOPE)
    redundant, _ = bind_text_query('slow charge ASIN B07P9S6R9Y 2022-09-03..2023-09-02', SCOPE)
    assert plain == redundant


def test_actual_failed_response_replayed_without_network(monkeypatch):
    import hashlib
    import json
    from pathlib import Path

    from app.agent.planner import LLMPlanner
    from app.agent.state import InvestigationState
    root = Path(__file__).resolve().parents[2] / '.local/acceptance-artifacts/internal-demo-real-quality-20261014-request64k-v13-final/paid'
    if not root.exists():
        pytest.skip('original local paid receipts unavailable')
    seal = json.loads((root / 'seal.json').read_text())
    for name in ('request-0002.json', 'response-0002.json'):
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == seal[name]
    raw = json.loads((root / 'response-0002.json').read_text())
    value = json.loads(next(c['text'] for o in raw['output'] for c in o.get('content', []) if c.get('text')))
    request = json.loads(json.loads((root / 'request-0002.json').read_text())['input'][1]['content'])
    monkeypatch.setenv('INVESTIGATION_PLANNER_PROTOCOL', 'native-v3')
    class RecordedGateway:
        execution_origin = 'controlled_fixture'
        last_model_receipt = {'raw_response': raw, 'replay_only': True}
        calls = 0
        def plan(self, **kw):
            self.calls += 1
            prompt = json.loads(kw['user'])
            assert '原始' in prompt['tool_capabilities']['text_evidence']
            return copy.deepcopy(value)
    gateway = RecordedGateway()
    decision = LLMPlanner(gateway).decide(InvestigationState(request_id='readonly-replay', user_query=request['query'],
        frozen_scope=request['frozen_scope'], observations=request['observations']), tuple(request['available_tools']))
    assert decision.arguments['query'] == '充电慢 买家反馈 原文 reviews'
    binding = gateway.last_model_receipt['query_scope_binding']
    assert binding['original_query'] == value['arguments']['query']
    assert not binding['scope_changed'] and gateway.calls == 1
    assert gateway.last_model_receipt['raw_response'] == raw
