"""Independent basis regressions, without evaluation answers or case identifiers."""
import pytest

from insight.investigation.fact_followup import metric_workplan, requested_metric_requests
from insight.investigation.planner import DeterministicPlanner


def save_trace(name, run):
    import json
    import os
    from pathlib import Path
    suffix = os.environ.get("INVESTIGATION_EVAL_REPORT_SUFFIX")
    if not suffix:
        return
    if not all(char.isalnum() or char in "_-" for char in suffix):
        raise ValueError("invalid artifact suffix")
    root = Path(__file__).resolve().parents[2] / ".local/acceptance-artifacts/development-occurrence"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}_{suffix}.json"
    with path.open("x") as handle:
        json.dump({"provenance": "SYNTHETIC independent CSV/API development regression, not model or business acceptance",
                   "run_id": str(run.pk), "status": run.status, "steps_used": run.steps_used,
                   "input_versions": run.input_versions, "result": run.result,
                   "tool_calls": list(run.tool_calls.values("tool_name", "authorization", "status", "output_refs"))}, handle, ensure_ascii=False, indent=2)


@pytest.mark.parametrize('query,expected', [
    ('只核对本月退款发生金额，不计算订单队列退款率', [('refunds', 'occurrence')]),
    ('核对本月收到退货件数，不比较成熟队列风险', [('returns', 'occurrence')]),
    ('Check refund amounts, do not calculate cohort rates', [('refunds', 'occurrence')]),
    ('Check received return units without comparing mature cohort risk', [('returns', 'occurrence')]),
    ('核对退款金额和成熟队列退款率', [('refunds', 'cohort'), ('refunds', 'occurrence')]),
    ('Compare refund cohort rates', [('refunds', 'cohort')]),
    ('核对退款金额和退货率', [('refunds', 'occurrence'), ('returns', 'cohort')]),
    ('Check refund amounts and return cohort rates', [('refunds', 'occurrence'), ('returns', 'cohort')]),
    ('return value and returning visitors', []),
])
def test_requested_basis_excludes_negated_calculations(query, expected):
    assert requested_metric_requests(query) == expected


def evidence(family='refunds', basis='received'):
    section = {'status': 'complete', 'baseline': {'event_basis': basis},
               'current': {'event_basis': basis, 'amount_by_currency': {'USD': '25.00', 'EUR': '-3.00'}}}
    return [{'type': 'metric_snapshot', 'status': 'complete', 'metrics': {
        'cohort': {'status': 'missing', 'reason': '缺少发货覆盖'},
        'occurrence': {family: section}}}]


def test_occurrence_remains_available_without_orders_and_multiple_currencies():
    plan = metric_workplan('只核对退款发生金额，不计算队列率', evidence())
    assert plan['completion_status'] == 'facts_available'
    assert plan['subtasks'][0]['fact_prefix'] == 'occurrence.refunds'
    end = DeterministicPlanner().decide(query='核对退款发生金额', evidence=evidence(), history=[], available_tools=[])
    assert end.business_contract['expected_stop_condition'] == 'stop_after_requested_frozen_facts'
    assert end.business_contract['expected_next_step'] == 'verify_frozen_requested_metric_facts'


@pytest.mark.parametrize('query,expected_tool', [
    ('只核对冻结的销量与流量日指标的基线窗、当前窗、完整状态与来源；不推断原因。', ''),
    ('Check only daily sales and traffic windows; do not infer causes.', ''),
    ('只比较冻结的销量与流量日指标的基线窗和当前窗，不推断原因，也不检索原文。', ''),
    ('Compare only frozen daily sales and traffic windows; do not infer causes or search records.', ''),
    ('核对销量和流量窗口，不判断经营原因。', ''),
    ('Check sales and sessions completeness, without inferring cause.', ''),
    ('分析销量下降原因。', 'text_evidence'),
    ('Why did sales decline?', 'text_evidence'),
    ('核对销量并查找运输受损证据。', 'text_evidence'),
    ('Check daily sales and search for shipping damage evidence.', 'text_evidence'),
])
def test_ready_daily_workplan_stops_only_for_pure_metric_intent(query, expected_tool):
    snapshot = {
        'type': 'metric_snapshot',
        'status': 'ready',
        'metrics': {'daily': {
            'status': 'ready',
            'baseline': {'start': '2026-08-20', 'end': '2026-09-16', 'complete': True},
            'current': {'start': '2026-09-17', 'end': '2026-09-23', 'complete': True},
        }},
        'metric_evidence': {'atoms': []},
    }
    decision = DeterministicPlanner().decide(
        query=query,
        evidence=[snapshot],
        history=[{'tool': 'metric_snapshot'}],
        available_tools=['text_evidence', 'review_claims'],
    )

    assert decision.next_tool == expected_tool
    if not expected_tool:
        assert decision.business_contract['expected_stop_condition'] == 'stop_after_requested_frozen_facts'
        subtasks = decision.business_basis['subtasks']
        assert len(subtasks) == 1
        assert (subtasks[0]['family'], subtasks[0]['mode'], subtasks[0]['status']) == (
            'daily', 'daily', 'facts_available'
        )
        assert subtasks[0]['actual_windows'] == [
            {'window': 'baseline', 'start': '2026-08-20', 'end': '2026-09-16'},
            {'window': 'current', 'start': '2026-09-17', 'end': '2026-09-23'},
        ]


def test_dual_basis_reports_partial_and_missing_occurrence_stays_blocked():
    plan = metric_workplan('核对退款金额和成熟队列退款率', evidence())
    assert plan['completion_status'] == 'partial'
    assert {(row['mode'], row['status']) for row in plan['subtasks']} == {('occurrence', 'facts_available'), ('cohort', 'blocked')}
    assert metric_workplan('核对退货件数', evidence())['completion_status'] == 'blocked'


def test_requested_return_units_cannot_prove_received_units():
    plan = metric_workplan('核对收到退货件数', evidence('returns', 'requested'))
    assert plan['completion_status'] == 'blocked'
    assert 'received_basis_not_available' in plan['subtasks'][0]['blocking_rules']
    assert metric_workplan('核对申请退货件数', evidence('returns', 'requested'))['completion_status'] == 'facts_available'
    assert metric_workplan('核对申请退货件数，不核对收到件数', evidence('returns', 'requested'))['completion_status'] == 'facts_available'


@pytest.mark.django_db(transaction=True)
def test_csv_calculator_worker_api_occurrence_without_orders(metric_scope, client):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from django.utils import timezone

    from insight.metrics.service import scan_object
    from insight.models import InvestigationRun
    from insight.tasks import run_job

    users, shops = metric_scope
    today = timezone.now().astimezone(ZoneInfo(shops[0].timezone)).date()
    start, end = today - timedelta(days=35), today - timedelta(days=1)
    rows = [{'business_date': str(end), 'seller_sku': 'S1', 'fulfillment': 'FBA',
             'record_id': f'TX{i}', 'order_line_id': 'PRIOR-MONTH-LINE', 'transaction_chain_id': 'CHAIN',
             'amount': amount, 'currency': currency}
            for i, (amount, currency) in enumerate([('40.00', 'USD'), ('-15.00', 'USD'), ('8.00', 'EUR')])]
    import_through_api(client, users['admin'], shops[0], 'refunds', rows, start, end)
    snapshot = scan_object(users['admin'], shops[0], 'B000TEST01', today=today)
    assert snapshot.payload['cohort']['status'] == 'missing'
    assert snapshot.payload['occurrence']['refunds']['current']['amount_by_currency'] == {'USD': '25.0000', 'EUR': '8.0000'}
    assert snapshot.payload['occurrence']['refunds']['current']['reversal_count'] == 1
    client.force_authenticate(users['admin'])
    for index, (query, status) in enumerate([
        ('核对退款发生金额，不计算成熟队列退款率', 'facts_available'),
        ('核对成熟队列退款率', 'blocked'),
        ('核对退款金额和成熟队列退款率', 'partial'),
    ]):
        response = client.post('/api/v1/investigations/', {'shop': str(shops[0].pk), 'child_asin': 'B000TEST01',
            'query': query, 'max_steps': 6, 'idempotency_key': f'occurrence-{index}'}, format='json')
        assert response.status_code == 202, response.data
        run_job(response.data['id'])
        run = InvestigationRun.objects.get(query=query)
        assert run.status == 'completed', (run.status, run.stop_reason)
        assert run.result['completion_status'] == status, run.result
        save_trace(f'occurrence_{index}', run)
        if status != 'blocked':
            row = next(task for task in run.result['subtasks'] if task['mode'] == 'occurrence')
            assert any(atom['path'].endswith('amount_by_currency.USD') for atom in row['citations'])


@pytest.mark.django_db(transaction=True)
def test_order_tool_api_requires_raw_source_access_and_hides_after_revocation(metric_scope, client):
    from insight.models import InvestigationRun, Membership, ShopGrant
    from insight.tasks import run_job
    users, shops = metric_scope
    payload = {'shop': str(shops[0].pk), 'child_asin': 'B000TEST01', 'query': '逐单核查订单明细',
               'max_steps': 6, 'idempotency_key': 'order-read'}
    client.force_authenticate(users['admin'])
    response = client.post('/api/v1/investigations/', payload, format='json')
    assert response.status_code == 202
    run_job(response.data['id'])
    run = InvestigationRun.objects.get(query=payload['query'])
    assert run.status == 'completed', run.stop_reason
    assert run.tool_calls.filter(tool_name='order_records', authorization='allowed').exists()
    assert next(item for item in run.result['evidence'] if item['type'] == 'order_records')['complete'] is False
    # Ordinary operator requests cannot read any order records.
    client.force_authenticate(users['a'])
    payload['idempotency_key'] = 'order-read-denied'
    denied = client.post('/api/v1/investigations/', payload, format='json')
    run_job(denied.data['id'])
    other = InvestigationRun.objects.get(requested_by=users['a'], query=payload['query'])
    assert not any(item.get('type') == 'order_records' for item in other.result.get('evidence', []))
    assert other.tool_calls.filter(tool_name='order_records', authorization='denied').exists()
    # Source grants are checked again on result/steps and the associated Job.
    Membership.objects.filter(user=users['admin']).update(is_admin=False)
    ShopGrant.objects.create(user=users['admin'], shop=shops[0], roles=['operator'])
    client.force_authenticate(users['admin'])
    assert client.get(f'/api/v1/investigations/{run.pk}/').status_code == 404
    assert client.get(f"/api/v1/jobs/{response.data['id']}/").status_code == 404


def import_through_api(client, user, shop, kind, rows, start, end, **contract):
    import csv
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile

    from insight.models import Job
    from insight.tasks import run_job

    client.force_authenticate(user)
    rows = [{'child_asin': 'B000TEST01', **row} for row in rows]
    fields = list(rows[0])
    source = client.post('/api/v1/sources/', {'shop': str(shop.pk), 'name': f'{kind}-occurrence', 'kind': kind,
        'is_primary': True, 'grain': 'product_record', 'column_mapping': {key: key for key in fields}, **contract}, format='json')
    assert source.status_code == 201, source.data
    handle = io.StringIO(newline='')
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    uploaded = client.post(f"/api/v1/sources/{source.data['id']}/upload/", {
        'file': SimpleUploadedFile('occurrence.csv', handle.getvalue().encode()), 'coverage_start': str(start),
        'coverage_end': str(end), 'generated_at': str(end) + 'T23:59:00Z', 'declared_complete': 'true',
        'declared_asins': ['B000TEST01']}, format='multipart')
    assert uploaded.status_code == 202, uploaded.data
    parse_job = Job.objects.get(kind='parse_import', payload__batch_id=uploaded.data['id'])
    run_job(str(parse_job.pk))
    parse_job.refresh_from_db()
    assert parse_job.status == 'completed', parse_job.error
    published = client.post(f"/api/v1/imports/{uploaded.data['id']}/publish/", {
        'allow_partial': False, 'confirm_contract': True}, format='json')
    assert published.status_code == 202, published.data
    run_job(published.data['id'])
    job = Job.objects.get(pk=published.data['id'])
    assert job.status == 'completed', job.error
    return source.data['id']


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('basis', ['requested', 'received'])
def test_return_csv_worker_calculator_basis_is_not_substituted(metric_scope, client, basis):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from django.utils import timezone

    from insight.metrics.service import scan_object
    users, shops = metric_scope
    today = timezone.now().astimezone(ZoneInfo(shops[0].timezone)).date()
    start, end = today - timedelta(days=35), today - timedelta(days=1)
    rows = [{'business_date': str(end), 'seller_sku': 'S1', 'fulfillment': 'FBA', 'record_id': 'RETURN1',
             'order_line_id': 'PREVIOUS-MONTH', 'quantity': 2, 'status': basis, 'requested_date': str(end),
             'received_date': str(end) if basis == 'received' else ''}]
    import_through_api(client, users['admin'], shops[0], 'returns', rows, start, end, return_event=basis)
    snapshot = scan_object(users['admin'], shops[0], 'B000TEST01', today=today)
    section = snapshot.payload['occurrence']['returns']
    assert section['current']['event_basis'] == basis
    assert section['current']['units'] == 2
    assert section['baseline']['event_basis'] == basis
    assert section['baseline']['units'] == 0
    assert metric_workplan(f'核对{"申请" if basis == "requested" else "收到"}退货件数', [{'type': 'metric_snapshot', 'metrics': snapshot.payload}])['completion_status'] == 'facts_available'
    assert metric_workplan('核对收到退货件数', [{'type': 'metric_snapshot', 'metrics': snapshot.payload}])['completion_status'] == ('facts_available' if basis == 'received' else 'blocked')


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('interrupt', [False, True])
def test_nonempty_order_lookup_recovery_preserves_frozen_records_and_budget(metric_scope, client, monkeypatch, interrupt):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from django.utils import timezone

    from insight.investigation import graph
    from insight.models import InvestigationRun
    from insight.tasks import run_job

    users, shops = metric_scope
    today = timezone.now().astimezone(ZoneInfo(shops[0].timezone)).date()
    end, prior = today - timedelta(days=1), today - timedelta(days=40)
    common = {'seller_sku': 'S1', 'fulfillment': 'FBA'}
    import_through_api(client, users['admin'], shops[0], 'orders', [{**common,
        'business_date': str(prior), 'record_id': 'LINE1', 'order_id': 'ORDER1', 'quantity': 2,
        'shipped_quantity': 2, 'shipped_date': str(prior), 'status': 'shipped'}], prior, end, coverage_basis='shipped_date')
    import_through_api(client, users['admin'], shops[0], 'refunds', [{**common,
        'business_date': str(end), 'record_id': 'REF1', 'order_line_id': 'LINE1', 'transaction_chain_id': 'CHAIN1',
        'amount': '12.00', 'currency': 'USD'}], prior, end)
    import_through_api(client, users['admin'], shops[0], 'returns', [{**common,
        'business_date': str(end), 'record_id': 'RET1', 'order_line_id': 'LINE1', 'quantity': 1,
        'status': 'received', 'requested_date': str(end), 'received_date': str(end)}], prior, end, return_event='received')
    payload = {'shop': str(shops[0].pk), 'child_asin': 'B000TEST01', 'query': '逐单核查订单明细',
               'max_steps': 6, 'idempotency_key': 'nonempty-orders'}
    response = client.post('/api/v1/investigations/', payload, format='json')
    original = graph._persist
    if interrupt:
        def interrupted(run, node, state, output, tool=''):
            if node == 'compose':
                raise RuntimeError('controlled_order_resume')
            return original(run, node, state, output, tool)
        monkeypatch.setattr(graph, '_persist', interrupted)
    run_job(response.data['id'])
    run = InvestigationRun.objects.get(query=payload['query'])
    if interrupt:
        assert run.status == 'partial'
        calls = list(run.tool_calls.values_list('pk', flat=True))
        used = run.steps_used
        monkeypatch.setattr(graph, '_persist', original)
        resumed = client.post(f'/api/v1/investigations/{run.pk}/resume/', {}, format='json')
        assert resumed.status_code == 202, resumed.data
        run_job(resumed.data['id'])
        run.refresh_from_db()
        assert list(run.tool_calls.values_list('pk', flat=True)) == calls
        assert run.steps_used == used + 1
    assert run.status == 'completed', run.stop_reason
    entry = next(item for item in run.result['evidence'] if item['type'] == 'order_records')
    assert entry['complete']
    assert entry['sections']['orders']['items'][0]['order_id'] == 'ORDER1'
    assert entry['sections']['refunds']['items'][0]['amount'] == '12.0000'
    assert entry['sections']['returns']['items'][0]['quantity'] == 1
    assert all(item['raw_row_id'] and item['revision'] == 1 for section in entry['sections'].values() for item in section['items'])
    assert run.tool_calls.filter(tool_name='order_records').count() == 1
    assert run.result['subtasks'][-1]['status'] == 'facts_available'
    save_trace(f'orders_{interrupt}', run)


def test_unknown_ratio_without_denominator_does_not_invalidate_missing_source_basis():
    atom = {"path": "daily.current.unit_session_percent", "denominator": None, "unit": "percent", "value": "null"}
    entry = {"type": "metric_snapshot", "metrics": {"daily": {"status": "missing"}}, "metric_evidence": {"atoms": [atom]}}
    plan = metric_workplan('核对销量', [entry])
    assert plan['subtasks'][0]['status'] == 'blocked'
    assert plan['subtasks'][0]['citations'] == []


@pytest.mark.django_db
def test_metric_run_detail_cannot_bypass_raw_order_identifier_permission(metric_scope, client):
    from insight.models import InvestigationRun
    users, shops = metric_scope
    run = InvestigationRun.objects.create(shop=shops[0], requested_by=users['a'], child_asin='B000TEST01',
        query='核对销量', status='completed', state={'metrics': {'order_ids': ['PRIVATE-ORDER'], 'denominator': 3}},
        result={'evidence': [{'metrics': {'cohort': {'refunds': {'current': {'order_ids': ['PRIVATE-ORDER'], 'denominator': 3}}}}}]})
    client.force_authenticate(users['a'])
    response = client.get(f'/api/v1/investigations/{run.pk}/')
    assert response.status_code == 200
    assert 'PRIVATE-ORDER' not in str(response.data)
    assert response.data['state']['metrics']['denominator'] == 3
    run.refresh_from_db()
    assert run.state['metrics']['order_ids'] == ['PRIVATE-ORDER']


@pytest.mark.django_db
def test_order_read_rejects_unbound_or_changed_source_manifest(metric_scope):
    from rest_framework.exceptions import ValidationError

    from insight.investigation.orders import read_order_records
    users, shops = metric_scope
    with pytest.raises(ValidationError, match='冻结来源'):
        read_order_records(actor=users['admin'], shop=shops[0], asin='B000TEST01', product_id=None,
                           manifest={'foreign-source': {'kind': 'orders', 'revision': 1}}, start='2026-09-01', end='2026-10-01')
