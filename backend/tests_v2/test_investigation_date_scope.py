"""Independent development date/boundary cases, no gold, qrels or expected-case lookup."""
from datetime import date

import pytest

from insight.investigation.citations import metric_catalog, validate_citations
from insight.investigation.fact_followup import metric_workplan
from insight.investigation.scope import resolve_scope, validate_scope_state


@pytest.mark.parametrize('query,start,end', [
    ('核对2025年1月实际收到的退货件数', '2025-01-01', '2025-01-31'),
    ('Inspect received returns in January 2025', '2025-01-01', '2025-01-31'),
    ('核对2024年2月退货', '2024-02-01', '2024-02-29'),
    ('refund amounts from 2025-01-01 to 2025-01-31', '2025-01-01', '2025-01-31'),
    ('核对2025-12-28至2026-01-01退款', '2025-12-28', '2026-01-01'),
    ('核对本月退款', '2026-01-01', '2026-01-01'),
    ('核对上月退款', '2025-12-01', '2025-12-31'),
    ('check last month refunds', '2025-12-01', '2025-12-31'),
    ('check this month refunds', '2026-01-01', '2026-01-01'),
    ('look for packaging damage in the last 7 days', '2025-12-26', '2026-01-01'),
])
def test_supported_ranges_resolve_at_frozen_site_anchor(query, start, end):
    result = resolve_scope(query, today=date(2026, 1, 1), timezone='America/Los_Angeles')
    assert result['status'] == 'explicit'
    assert (result['start'], result['end']) == (start, end)
    assert result['anchor_date'] == '2026-01-01'
    assert result['timezone'] == 'America/Los_Angeles'


@pytest.mark.parametrize('query', [
    '核对2025年1月和2025年2月退款', 'Compare January 2025 and February 2025 refunds',
    '不要核对2025年1月退款', 'Check refunds excluding January 2025',
    '核对2025-02-29退货', '核对2025-13退款', '核对去年退款',
    'Compare refunds last week', '核对1月退货', '核对2026-01-10至2026-01-01退货',
    'Find records in the last 400 days',
])
def test_ambiguous_negated_invalid_or_unsupported_dates_do_not_use_default(query):
    result = resolve_scope(query, today='2026-01-01')
    assert result['status'] == 'needs_clarification'
    assert result['start'] is None and result['end'] is None


def snapshot():
    metrics = {'occurrence': {'returns': {'status': 'ready',
        'baseline': {'start': '2026-09-01', 'end': '2026-09-14', 'units': 4, 'event_basis': 'received'},
        'current': {'start': '2026-09-15', 'end': '2026-09-28', 'units': 2, 'event_basis': 'received'}}}}
    return {'type': 'metric_snapshot', 'metrics': metrics, 'metric_evidence': metric_catalog(
        snapshot_id='snapshot', snapshot_fingerprint='fingerprint', source_manifest={}, shop_id='shop',
        child_asin='B000TEST01', product_id=None, metrics=metrics)}


def test_reported_wrong_window_reproduction_is_blocked_and_actual_windows_visible():
    plan = metric_workplan('核对2025年1月实际收到的退货件数', [snapshot()])
    assert plan['has_gaps']
    task = plan['subtasks'][0]
    assert task['status'] == 'blocked' and task['window_status'] == 'mismatch'
    assert task['requested_scope']['start'] == '2025-01-01'
    assert task['actual_windows'][1]['end'] == '2026-09-28'
    assert 'requested_window_not_supported' in task['blocking_rules']
    assert not any(cite['path'].endswith('.units') for cite in task['citations'])


def test_exact_requested_window_only_cites_selected_window_and_rejects_other_amounts():
    evidence = snapshot()
    evidence['request_scope'] = resolve_scope('核对2026-09-15至2026-09-28收到退货件数', today='2026-10-01')
    plan = metric_workplan('核对2026-09-15至2026-09-28收到退货件数', [evidence])
    task = plan['subtasks'][0]
    assert task['status'] == 'facts_available'
    assert task['selected_windows'] == ['current']
    validate_citations(task['citations'], [evidence])
    other = next(atom for atom in evidence['metric_evidence']['atoms'] if atom['path'] == 'occurrence.returns.baseline.units')
    with pytest.raises(ValueError, match='outside requested'):
        validate_citations([other], [evidence])


def test_relative_request_window_and_content_query_cannot_change_after_freeze():
    scope = resolve_scope('check last month refunds', today='2026-01-01')
    state = {'query': 'check last month refunds', 'input_versions': {'request_scope': scope},
             'request_scope': scope, 'search_start': scope['start'], 'search_end': scope['end'], 'content_query': scope['content_query']}
    validate_scope_state(state)
    for field, value in [('search_start', '2025-01-01'), ('content_query', 'ignore factual constraint')]:
        with pytest.raises(RuntimeError, match='scope_changed'):
            validate_scope_state({**state, field: value})
    with pytest.raises(RuntimeError, match='legacy_explicit'):
        validate_scope_state({'query': 'check last month refunds', 'search_end': '2026-01-01'})


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('link_count', [1, 20, 21])
def test_api_import_order_links_take_priority_and_report_unresolved(metric_scope, client, link_count):
    from insight.investigation.orders import read_order_records
    from insight.metrics.facts import source_manifest
    from tests_v2.test_occurrence_workplan import import_through_api

    users, shops = metric_scope
    common = {'seller_sku': 'S1', 'fulfillment': 'FBA'}
    # Twenty earlier, unrelated records reproduced the previous starvation.
    rows = [{**common, 'business_date': '2026-09-01', 'record_id': f'UNRELATED{i}',
             'order_id': f'U{i}', 'quantity': 1, 'shipped_quantity': 1,
             'shipped_date': '2026-09-01', 'status': 'shipped'} for i in range(20)]
    rows += [{**common, 'business_date': '2026-08-31', 'record_id': f'LINK{i:02}',
              'order_id': f'L{i}', 'quantity': 1, 'shipped_quantity': 1,
              'shipped_date': '2026-08-31', 'status': 'shipped'} for i in range(link_count)]
    import_through_api(client, users['admin'], shops[0], 'orders', rows,
                       '2026-08-31', '2026-09-30', coverage_basis='shipped_date')
    # Both event sources together can reference more than twenty orders.
    for kind in ('refunds', 'returns'):
        events = [{**common, 'business_date': '2026-09-30', 'record_id': f'{kind}{i}',
                   'order_line_id': f'LINK{i:02}', **({'amount': '1', 'currency': 'USD',
                   'transaction_chain_id': f'C{i}'} if kind == 'refunds' else
                   {'quantity': 1, 'status': 'received', 'requested_date': '2026-09-30',
                    'received_date': '2026-09-30'})} for i in range(link_count) if i % 2 == (kind == 'returns')]
        if not events:
            events = [{**common, 'business_date': '2026-09-30', 'record_id': 'return0',
                       'order_line_id': 'LINK00', 'quantity': 1, 'status': 'received',
                       'requested_date': '2026-09-30', 'received_date': '2026-09-30'}]
        import_through_api(client, users['admin'], shops[0], kind, events,
                           '2026-08-31', '2026-09-30', **({'return_event': 'received'} if kind == 'returns' else {}))
    result = read_order_records(actor=users['admin'], shop=shops[0], asin='B000TEST01',
        product_id=None, manifest=source_manifest(shops[0]), start='2026-09-01', end='2026-09-30')
    selected = result['sections']['orders']['items']
    assert len(selected) == 20
    assert sum(item['association_only'] for item in selected) == min(link_count, 20)
    assert len(result['associations']['unresolved_order_line_ids']) == max(0, link_count - 20)
    assert not result['complete']  # Unrelated in-window records still exceed budget.
    assert result['sections']['orders']['status'] == 'sampled'


@pytest.mark.django_db(transaction=True)
def test_explicit_window_successor_freezes_inherited_anchor_before_tools(metric_scope):
    from insight.derived import refresh_derived
    from insight.models import DerivedRefresh, InvestigationRun
    from tests_v2.test_derived_refresh import revision

    users, shops = metric_scope
    scope = resolve_scope('核对上月退款金额', today='2026-01-01', timezone=shops[0].timezone)
    original = InvestigationRun.objects.create(shop=shops[0], requested_by=users['a'],
        child_asin='B000TEST01', query='核对上月退款金额', status='completed', max_steps=6,
        state={'request_scope': scope, 'search_start': scope['start'], 'search_end': scope['end']})
    revision(metric_scope)
    receipt = DerivedRefresh.objects.get(artifact_id=original.pk)
    result = refresh_derived(receipt.job, lambda: None)
    assert result['status'] == 'completed', result
    successor = InvestigationRun.objects.get(pk=result['replacement_id'])
    assert successor.input_versions['request_scope'] == scope
    assert successor.state['search_start'] == '2025-12-01'
    assert successor.state['search_end'] == '2025-12-31'
    assert successor.result['subtasks'][0]['window_status'] == 'mismatch'
    assert successor.previous_run_id == original.pk


def test_single_received_window_does_not_borrow_other_window_basis():
    evidence = snapshot()
    evidence['metrics']['occurrence']['returns']['baseline']['event_basis'] = 'requested'
    query = '核对2026-09-15至2026-09-28收到退货件数'
    evidence['request_scope'] = resolve_scope(query, today='2026-10-01')
    task = metric_workplan(query, [evidence])['subtasks'][0]
    assert task['status'] == 'facts_available'
    assert task['selected_windows'] == ['current']


@pytest.mark.parametrize('query', ['refunds since January 2025', '核对2025年1月以前退款',
                                   'returns before 2025-01-31', '核对退款，不包括2025年1月'])
def test_open_ended_or_excluded_dates_require_complete_range(query):
    assert resolve_scope(query, today='2026-10-08')['status'] == 'needs_clarification'


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('matching', [True, False])
def test_csv_worker_metric_investigation_preserves_requested_window(metric_scope, client, matching):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from django.utils import timezone

    from insight.metrics.service import scan_object
    from insight.models import InvestigationRun
    from insight.tasks import run_job
    from tests_v2.test_occurrence_workplan import import_through_api, save_trace

    users, shops = metric_scope
    today = timezone.now().astimezone(ZoneInfo(shops[0].timezone)).date()
    end, start = today - timedelta(days=1), today - timedelta(days=35)
    import_through_api(client, users['admin'], shops[0], 'returns', [{
        'business_date': str(end), 'seller_sku': 'S1', 'fulfillment': 'FBA',
        'record_id': 'DATE-RETURN', 'order_line_id': 'HISTORICAL', 'quantity': 2,
        'status': 'received', 'requested_date': str(end), 'received_date': str(end)}],
        start, end, return_event='received')
    snapshot_row = scan_object(users['admin'], shops[0], 'B000TEST01', today=today)
    window = snapshot_row.payload['occurrence']['returns']['current']
    query = f"核对{window['start']}至{window['end']}收到退货件数" if matching else '核对2025年1月收到退货件数'
    response = client.post('/api/v1/investigations/', {'shop': str(shops[0].pk),
        'child_asin': 'B000TEST01', 'query': query, 'max_steps': 6,
        'idempotency_key': f'date-match-{matching}'}, format='json')
    assert response.status_code == 202, response.data
    run_job(response.data['id'])
    run = InvestigationRun.objects.get(query=query)
    assert run.status == 'completed', run.stop_reason
    scope = run.input_versions['request_scope']
    assert scope['start'] == (window['start'] if matching else '2025-01-01')
    assert run.state['search_start'] == scope['start']
    assert run.result['request_scope'] == scope
    task = run.result['subtasks'][0]
    assert task['window_status'] == ('matched' if matching else 'mismatch')
    assert task['status'] == ('facts_available' if matching else 'blocked')
    assert any(atom['path'].endswith('.units') for atom in task['citations']) == matching
    detail = client.get(f'/api/v1/investigations/{run.pk}/')
    assert detail.status_code == 200
    assert detail.data['result']['request_scope'] == scope
    save_trace(f'date_metric_{matching}', run)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('query', ['查找2025年1月包装评论原文', 'Find packaging reviews in January 2025'])
def test_review_title_body_and_claims_are_scoped_in_actual_graph(metric_scope, client, query):
    from insight.metrics.facts import source_manifest
    from insight.models import InvestigationRun, Review, SemanticClaim, SemanticRun
    from insight.tasks import run_job
    from tests_v2.test_occurrence_workplan import import_through_api, save_trace

    users, shops = metric_scope
    from insight.models import ProductMapping
    ProductMapping.objects.filter(product__shop=shops[0]).update(effective_from='2024-01-01')
    rows = [{'business_date': day, 'seller_sku': 'S1', 'fulfillment': 'FBA', 'record_id': record,
             'title': 'Packaging damage', 'text': body, 'rating': 2, 'language': 'en',
             'collected_at': day + 'T12:00:00Z'} for day, record, body in [
                 ('2024-12-31', 'BEFORE', 'Packaging broken before window'),
                 ('2025-01-01', 'START', 'Packaging crushed start'),
                 ('2025-01-31', 'END', 'Packaging dented end'),
                 ('2025-02-01', 'AFTER', 'Packaging broken after window')]]
    import_through_api(client, users['admin'], shops[0], 'reviews', rows, '2024-12-31', '2025-02-01')
    semantic = SemanticRun.objects.create(shop=shops[0], requested_by=users['admin'],
        child_asin='B000TEST01', status='completed', source_manifest=source_manifest(shops[0]), provider='test')
    for review in Review.objects.filter(source__shop=shops[0]):
        SemanticClaim.objects.create(run=semantic, shop=shops[0], review=review,
            prediction_id=review.record_id, secondary_topic='packaging', evidence_span=review.title,
            prediction_status='accepted', polarity='negative')
    response = client.post('/api/v1/investigations/', {'shop': str(shops[0].pk),
        'child_asin': 'B000TEST01', 'query': query, 'max_steps': 6,
        'idempotency_key': 'text-dates'}, format='json')
    assert response.status_code == 202, response.data
    run_job(response.data['id'])
    run = InvestigationRun.objects.get(query=query)
    assert run.status == 'completed', run.stop_reason
    if query.startswith('Find'):
        assert run.tool_calls.filter(tool_name='review_claims').exists()
        assert {item['business_date'] for item in run.state['hypotheses']} == {'2025-01-01', '2025-01-31'}
    texts = [item for item in run.result['evidence'] if item['type'] == 'text_retrieval']
    assert texts
    observed = {item['record_id'] for entry in texts for item in entry['items']}
    assert observed == {'START', 'END'}
    for entry in texts:
        assert entry['scope']['start'] == '2025-01-01'
        assert entry['scope']['end'] == '2025-01-31'
        assert entry['request_scope'] == run.input_versions['request_scope']
        for item in entry['items']:
            assert item['title'] == 'Packaging damage'
    save_trace('date_text_' + ('cn' if query.startswith('查找') else 'en'), run)


@pytest.mark.parametrize('query', [
    '检查最近两周的退款', 'Check refunds in the last 2 weeks', 'Check refunds since Monday',
    '最近两个月退款', '过去三年退货', '上个星期退款', 'last two months refunds',
    'previous 3 years returns', '最近2 weeks退款', 'last 两周 refunds',
    '核对周一退款', 'before Friday returns', 'after shipment refunds',
    '本月退款和最近两周订单', 'January 2025 refunds and last two weeks returns',
    '不统计最近两周退款', 'excluding last 2 weeks refunds',
    'last 2 business days refunds', '三天前的退款', 'refunds in the last quarter',
    'last two fiscal years refunds', 'past 3 calendar weeks returns',
    '检查最近两个自然月退款', 'last couple of working days refunds',
])
def test_time_constraint_detection_never_falls_back_when_range_is_unparsed(query):
    scope = resolve_scope(query, today='2026-10-08')
    assert scope['status'] == 'needs_clarification'
    assert scope['start'] is None and scope['end'] is None


@pytest.mark.parametrize('query', [
    'Check Monday charger reviews', 'May I inspect refunds?', '检查年度会员产品退款',
    '核对订单号 ORD-2025-01-01 的明细', 'Check SKU 2025-01-01 refund records',
    '查找原文“最近两周总是坏”，核实产品质量',
    'Find review says "since Monday broken" evidence',
    "Find review says 'since Monday broken' evidence",
    'Inspect reviews before citing evidence',
])
def test_non_date_products_identifiers_and_attributed_source_quotes_do_not_set_scope(query):
    assert resolve_scope(query, today='2026-10-08')['status'] == 'default'


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('query', ['检查最近两周的退款和订单明细', 'Check refunds since Monday and review evidence'])
def test_unparsed_time_api_worker_clarifies_before_any_data_tool(metric_scope, client, query):
    from insight.models import InvestigationRun
    from insight.tasks import run_job
    users, shops = metric_scope
    client.force_authenticate(users['admin'])
    response = client.post('/api/v1/investigations/', {'shop': str(shops[0].pk),
        'child_asin': 'B000TEST01', 'query': query, 'max_steps': 6,
        'idempotency_key': 'unparsed-time'}, format='json')
    assert response.status_code == 202
    run_job(response.data['id'])
    run = InvestigationRun.objects.get(query=query)
    assert run.status == 'needs_clarification'
    assert run.result['request_scope']['status'] == 'needs_clarification'
    assert not run.tool_calls.exists()
    assert run.steps_used == 1
    assert 'search_start' not in run.state and 'search_end' not in run.state
    detail = client.get(f'/api/v1/investigations/{run.pk}/')
    assert detail.status_code == 200
    assert detail.data['result']['request_scope']['start'] is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("interruption", [None, "partial", "cancelled"])
def test_date_clarification_new_run_uses_correction_and_preserves_budget_and_parent(metric_scope, client, monkeypatch, interruption):
    from insight.models import InvestigationRun
    from insight.tasks import run_job
    users, shops = metric_scope
    from insight.models import Product, ProductMapping
    chosen = Product.objects.get(shop=shops[0])
    other = Product.objects.create(shop=shops[0], child_asin='B000TEST01', seller_sku='S2', fulfillment='FBM')
    ProductMapping.objects.create(product=other, name='Other SKU', effective_from='2025-01-01', version=1, reason='fixture')
    client.force_authenticate(users['admin'])
    response = client.post('/api/v1/investigations/', {'shop': str(shops[0].pk),
        'child_asin': 'B000TEST01', 'product': str(chosen.pk), 'query': '核对最近两周退款金额', 'max_steps': 6,
        'idempotency_key': 'date-clarify-parent'}, format='json')
    run_job(response.data['id'])
    parent = InvestigationRun.objects.get(query='核对最近两周退款金额')
    assert parent.status == 'needs_clarification' and parent.steps_used == 1
    before = parent.result
    corrected = client.post(f'/api/v1/investigations/{parent.pk}/clarify/', {
        'query': '2025-01-01至2025-01-31', 'idempotency_key': 'date-correction'}, format='json')
    assert corrected.status_code == 202, corrected.data
    replay = client.post(f'/api/v1/investigations/{parent.pk}/clarify/', {
        'query': '2025-01-01至2025-01-31', 'idempotency_key': 'date-correction'}, format='json')
    assert replay.status_code == 202 and replay.data['id'] == corrected.data['id']
    changed = client.post(f'/api/v1/investigations/{parent.pk}/clarify/', {
        'query': '2024-01-01至2024-01-31', 'idempotency_key': 'date-correction'}, format='json')
    assert changed.status_code == 409
    assert parent.revisions.count() == 1
    from insight.investigation import graph
    original_persist = graph._persist
    if interruption:
        def stop_before_compose(run, node, state, output, tool=""):
            if node == "compose":
                raise RuntimeError("controlled_clarification_interruption")
            return original_persist(run, node, state, output, tool)
        monkeypatch.setattr(graph, '_persist', stop_before_compose)
    run_job(corrected.data['id'])
    child = parent.revisions.get()
    if interruption:
        assert child.status == 'partial'
        frozen = child.state['request_scope']
        used, budget = child.steps_used, child.budget_units
        calls = child.tool_calls.count()
        if interruption == 'cancelled':
            cancelled = client.post(f'/api/v1/investigations/{child.pk}/cancel/')
            assert cancelled.status_code == 200
            child.refresh_from_db()
            assert child.status == 'cancelled' and child.budget_units == budget
        monkeypatch.setattr(graph, '_persist', original_persist)
        resumed = client.post(f'/api/v1/investigations/{child.pk}/resume/')
        assert resumed.status_code == 202, resumed.data
        run_job(resumed.data['id'])
        child.refresh_from_db()
        assert child.state['request_scope'] == frozen
        assert child.steps_used >= used and child.budget_units <= budget
        assert child.tool_calls.count() == calls

    assert child.status == 'completed', child.stop_reason
    assert child.child_asin == parent.child_asin and child.product_id == parent.product_id == chosen.pk
    assert child.max_steps == 5 and child.steps_used + parent.steps_used <= parent.max_steps
    scope = child.input_versions['request_scope']
    assert scope['status'] == 'explicit' and scope['start'] == '2025-01-01' and scope['end'] == '2025-01-31'
    assert scope['resolution_query'] == '2025-01-01至2025-01-31'
    assert '退款金额' in scope['content_query'] and '最近两周' not in scope['content_query']
    assert child.tool_calls.filter(tool_name='metric_snapshot').exists()
    assert child.result['subtasks'][0]['family'] == 'refunds'
    parent.refresh_from_db()
    assert parent.result == before and parent.status == 'needs_clarification' and not parent.tool_calls.exists()
    state = child.state
    validate_scope_state(state)
    with pytest.raises(RuntimeError, match='scope_changed'):
        validate_scope_state({**state, 'scope_query': '2024-01-01至2024-01-31'})
    if interruption is None:
        from insight.derived import refresh_derived
        from insight.models import DerivedRefresh
        from tests_v2.test_derived_refresh import revision
        frozen, remaining = child.state['request_scope'], child.budget_units
        revision(metric_scope)
        receipt = DerivedRefresh.objects.get(artifact_id=child.pk)
        refreshed = refresh_derived(receipt.job, lambda: None)
        successor = InvestigationRun.objects.get(pk=refreshed['replacement_id'])
        assert successor.read_only_refresh and successor.product_id == chosen.pk
        assert successor.state['scope_query'] == child.state['scope_query']
        assert successor.state['request_scope'] == frozen
        assert successor.budget_units <= remaining
        assert refreshed['status'] == ('completed' if successor.status == 'completed' else 'needs_confirmation')
        child.refresh_from_db()
        assert child.state == state and child.budget_units == remaining



def test_date_correction_content_keeps_source_quote_but_removes_old_period():
    from insight.investigation.scope import corrected_content_query
    result = corrected_content_query('Find packaging reviews in the last 2 weeks; review says "since Monday broken"; 2025-01-01 to 2025-01-31')
    assert 'packaging' in result and 'since Monday broken' in result
    assert 'last 2 weeks' not in result and '2025-01-01' not in result


@pytest.mark.django_db(transaction=True)
def test_date_clarification_without_date_does_not_fall_back_to_default(metric_scope, client):
    from insight.models import InvestigationRun
    from insight.tasks import run_job
    users, shops = metric_scope
    client.force_authenticate(users['admin'])
    first = client.post('/api/v1/investigations/', {'shop': str(shops[0].pk),
        'child_asin': 'B000TEST01', 'query': 'Check refunds since Monday', 'max_steps': 6,
        'idempotency_key': 'missing-date-correction'}, format='json')
    run_job(first.data['id'])
    parent = InvestigationRun.objects.get(query='Check refunds since Monday')
    response = client.post(f'/api/v1/investigations/{parent.pk}/clarify/', {
        'query': 'B000TEST01', 'child_asin': 'B000TEST01', 'idempotency_key': 'only-asin'}, format='json')
    assert response.status_code == 202, response.data
    run_job(response.data['id'])
    child = parent.revisions.get()
    assert child.status == 'needs_clarification'
    assert child.result['request_scope']['start'] is None
    assert not child.tool_calls.exists()
    assert child.max_steps == 5 and child.steps_used == 1


@pytest.mark.django_db(transaction=True)
def test_date_correction_rechecks_changed_product_and_revoked_parent_access(metric_scope, client):
    from insight.models import InvestigationRun, Product, ProductMapping, ShopGrant
    from insight.tasks import run_job
    users, shops = metric_scope
    chosen = Product.objects.get(shop=shops[0])
    other = Product.objects.create(shop=shops[0], child_asin='B000TEST01', seller_sku='S2', fulfillment='FBM')
    ProductMapping.objects.create(product=other, name='Other SKU', effective_from='2025-01-01', version=1, reason='fixture')
    grant = ShopGrant.objects.get(user=users['restricted'])
    grant.products.add(chosen)
    client.force_authenticate(users['restricted'])
    first = client.post('/api/v1/investigations/', {'shop': str(shops[0].pk),
        'child_asin': chosen.child_asin, 'product': str(chosen.pk), 'query': '最近两周退款',
        'max_steps': 6, 'idempotency_key': 'restricted-date-parent'}, format='json')
    assert first.status_code == 202, first.data
    run_job(first.data['id'])
    parent = InvestigationRun.objects.get(pk=first.data['result']['investigation_id'])
    request = {'query': '2025-01-01至2025-01-31', 'product': str(other.pk), 'idempotency_key': 'restricted-correction'}
    rejected = client.post(f'/api/v1/investigations/{parent.pk}/clarify/', request, format='json')
    assert rejected.status_code in {403, 404}
    assert not parent.revisions.exists()
    request['product'] = str(chosen.pk)
    accepted = client.post(f'/api/v1/investigations/{parent.pk}/clarify/', request, format='json')
    assert accepted.status_code == 202, accepted.data
    grant.products.clear()
    replay = client.post(f'/api/v1/investigations/{parent.pk}/clarify/', request, format='json')
    assert replay.status_code in {403, 404}
    assert parent.revisions.count() == 1
    parent.refresh_from_db()
    assert parent.status == 'needs_clarification' and not parent.tool_calls.exists()
