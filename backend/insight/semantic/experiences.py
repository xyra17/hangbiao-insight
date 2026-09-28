"""Frozen, object-scoped experience linkage; textual similarity is never an identity."""
from collections import defaultdict

from insight.ingestion.contracts import digest
from insight.metrics.facts import latest_facts


def experience_links(material, start, end):
    manifest = material.source_manifest
    if not isinstance(manifest, dict) or any(
        not isinstance(v, dict) or not {"kind", "revision"} <= v.keys() for v in manifest.values()
    ):
        return {"status": "unavailable", "groups": [], "unlinked": [], "source_counts": {}}

    def rows(kind):
        return latest_facts(manifest, kind, material.child_asin, material.product_id).filter(
            source__shop_id=material.shop_id
        ).select_related("raw_row", "batch__contract")

    # Orders may precede the observation window. Only references explicitly present
    # in the frozen source and this object can resolve an experience.
    orders = defaultdict(list)
    for row in rows("orders").filter(business_date__lte=end):
        orders[row.record_id].append(row)
    groups, unlinked, counts = {}, [], {}
    for kind in ("returns", "refunds", "support", "reviews"):
        records = rows(kind).filter(business_date__range=(start, end))
        if kind == "reviews":
            records = records.filter(is_deleted=False)
        counts[kind] = 0
        for row in records:
            counts[kind] += 1
            ref = {
                "source_type": kind, "record_id": row.record_id,
                "raw_row_id": str(row.raw_row_id), "batch_id": str(row.batch_id),
                "line": row.raw_row.line, "business_date": str(row.business_date),
                "revision": row.revision,
            }
            line_id = getattr(row, "order_line_id", "")
            matches = orders.get(line_id, []) if line_id else []
            if len(matches) != 1 or not matches[0].order_id or matches[0].is_exchange:
                reason = ("review_has_no_order_link" if kind == "reviews" else
                          "missing_order_line" if not line_id else
                          "ambiguous_order_line" if len(matches) > 1 else
                          "exchange_requires_original_link" if matches and matches[0].is_exchange else
                          "order_not_resolved")
                unlinked.append({**ref, "reason": reason})
                continue
            order = matches[0]
            identity = digest({"shop": str(material.shop_id), "asin": material.child_asin,
                               "product": str(material.product_id or ""), "order": order.order_id})
            group = groups.setdefault(identity, {
                "experience_id": identity, "order_id": order.order_id,
                "order_refs": [], "records": [], "independent_experiences": 1,
            })
            order_ref = str(order.raw_row_id)
            if order_ref not in group["order_refs"]:
                group["order_refs"].append(order_ref)
            group["records"].append(ref)
    return {
        "status": "partial" if unlinked else "linked" if groups else "no_records",
        "window_start": str(start), "window_end": str(end),
        "source_counts": counts, "linked_order_count": len(groups),
        "groups": sorted(groups.values(), key=lambda g: g["experience_id"]),
        "unlinked": unlinked, "independence": "unknown" if unlinked else "order_identity_only",
        "limitations": [
            "关联仅证明同一订单经历，不证明同一问题或独立买家；退款记录数量不是有效退款订单数。",
            "未关联来源分别计数，不相加为受影响订单；评论不凭措辞或日期关联订单。",
        ],
    }
