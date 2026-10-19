"""Bounded, frozen, source-authorized order records; no financial inference."""
from datetime import date

from insight.ingestion.service import require_source_access
from insight.metrics.facts import authorize_object, latest_facts, source_for, source_manifest

LIMIT = 20
FIELDS = {
    'orders': ('record_id', 'order_id', 'quantity', 'shipped_date', 'shipped_quantity', 'original_line_id', 'is_exchange', 'status'),
    'refunds': ('record_id', 'order_line_id', 'transaction_chain_id', 'amount', 'currency', 'quantity'),
    'returns': ('record_id', 'order_line_id', 'quantity', 'status', 'requested_date', 'received_date'),
}


def read_order_records(*, actor, shop, asin, product_id, manifest, start, end):
    require_source_access(actor, shop)
    authorize_object(actor, shop, asin, product_id)
    if source_manifest(shop) != manifest:
        from rest_framework.exceptions import ValidationError
        raise ValidationError("订单记录的冻结来源与当前授权来源版本不匹配；需重新冻结调查。")
    start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    sections = {}
    linked = set()
    unresolved = []
    for kind in ('refunds', 'returns', 'orders'):
        source_id, revision = source_for(manifest, kind)
        if not source_id:
            sections[kind] = {'status': 'missing', 'items': [], 'complete': False}
            continue
        qs = latest_facts(manifest, kind, asin, product_id)
        association_rows = []
        if kind == 'orders':
            # Linked rows take priority over unrelated in-window orders.
            association_rows = list(qs.filter(record_id__in=linked).order_by('record_id', 'business_key', 'pk')[:LIMIT + 1])
            chosen = association_rows[:LIMIT]
            remaining = LIMIT - len(chosen)
            window_rows = list(qs.filter(business_date__range=(start, end)).exclude(pk__in=[row.pk for row in chosen])
                               .order_by('business_date', 'business_key', 'pk')[:remaining + 1])
            rows = chosen + window_rows
            unresolved = sorted(linked - {row.record_id for row in chosen})
        else:
            rows = list(qs.filter(business_date__range=(start, end)).order_by('business_date', 'business_key', 'pk')[:LIMIT + 1])
        items = []
        for row in rows[:LIMIT]:
            values = {key: getattr(row, key) for key in FIELDS[kind]}
            values = {key: value if value is None or isinstance(value, (bool, int)) else str(value) for key, value in values.items()}
            items.append({**values, 'fact_id': str(row.pk), 'source_id': str(row.source_id),
                          'association_only': kind == 'orders' and not start <= row.business_date <= end,
                          'revision': row.revision, 'raw_row_id': str(row.raw_row_id), 'business_date': row.business_date.isoformat()})
            if kind != 'orders' and row.order_line_id:
                linked.add(row.order_line_id)
        truncated = len(rows) > LIMIT or len(association_rows) > LIMIT
        sections[kind] = {'status': 'sampled' if truncated else 'unresolved' if kind == 'orders' and unresolved else 'read', 'complete': not truncated and not (kind == 'orders' and unresolved),
                          'source_id': source_id, 'revision': revision, 'items': items}
    if sections.get('orders', {}).get('status') == 'missing':
        unresolved = sorted(linked)
    complete = all(section['complete'] for section in sections.values())
    return {'type': 'order_records', 'status': 'facts_available' if complete else 'partial',
            'scope': {'shop_id': str(shop.pk), 'child_asin': asin, 'product_id': str(product_id) if product_id else None,
                      'start': start.isoformat(), 'end': end.isoformat()}, 'sections': sections,
            'limit_per_source': LIMIT, 'complete': complete,
            'associations': {'requested_order_line_ids': sorted(linked), 'unresolved_order_line_ids': unresolved,
                             'status': 'unresolved' if unresolved else 'observed_sample_links',
                             'exception': '仅补读样本引用的同对象历史订单；association_only记录不计入当期事件。'},
            'limitations': ['仅核查冻结版本与限定窗口的原记录及样本关联；不计算队列率、不推断交易链完整性或总体风险。',
                            '完整表示所选范围未达到样本上限，不表示来源覆盖完整；跨月订单仅补读样本中引用的同对象订单行。']}


def redact_order_ids(value):
    """Metric facts remain readable without exposing raw order identifiers."""
    if isinstance(value, dict):
        return {key: redact_order_ids(item) for key, item in value.items() if key != "order_ids"}
    if isinstance(value, list):
        return [redact_order_ids(item) for item in value]
    return value
