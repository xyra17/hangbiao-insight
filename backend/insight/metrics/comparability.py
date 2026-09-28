from collections import defaultdict
from datetime import date

from django.db.models import Q

from insight.models import ProductMapping

from .facts import latest_facts, source_for


def assess_comparability(manifest, asin, daily, cohort, product_id=None):
    windows = []
    for label, section in [
        ("销售与流量", daily),
        ("退款队列", cohort.get("refunds", {})),
        ("退货队列", cohort.get("returns", {})),
    ]:
        baseline, current = section.get("baseline"), section.get("current")
        if baseline and current:
            windows.append({"label": label, "start": baseline["start"], "end": current["end"]})
    result = {
        "status": "unknown",
        "windows": windows,
        "operations": [],
        "structure_changes": [],
        "limitations": [],
    }
    if not windows:
        result["limitations"].append("尚无完整比较窗口，无法核对同期运营变化。")
        return result
    start = min(date.fromisoformat(window["start"]) for window in windows)
    end = max(date.fromisoformat(window["end"]) for window in windows)
    operation_source, _ = source_for(manifest, "operations")
    if not operation_source:
        result["limitations"].append("未接入运营事件来源，促销、调价及补货等干扰因素未知。")
    else:
        for row in (
            latest_facts(manifest, "operations", asin, product_id)
            .filter(business_date__lte=end)
            .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=start))
        ):
            overlap = [
                window["label"]
                for window in windows
                if row.business_date <= date.fromisoformat(window["end"])
                and (row.ends_on is None or row.ends_on >= date.fromisoformat(window["start"]))
            ]
            if not overlap:
                continue
            result["operations"].append(
                {
                    "type": row.event_type,
                    "description": row.description,
                    "start": str(row.business_date),
                    "end": str(row.ends_on) if row.ends_on else None,
                    "raw_row_id": str(row.raw_row_id),
                    "windows": overlap,
                }
            )
        result["limitations"].append("无记录不证明没有运营变化；未提供结束日期的事件暂按可能持续影响显示。")
    # ASIN-native rows have no single mapping FK; resolve each SKU using the
    # mapping versions captured by that import, not the current parent mapping.
    mappings = defaultdict(list)
    rows_by_kind = {
        kind: list(
            latest_facts(manifest, kind, asin, product_id)
            .filter(business_date__range=(start, end))
            .select_related("mapping", "batch")
        )
        for kind in ("sales_traffic", "orders")
    }
    identifiers = {
        identifier
        for rows in rows_by_kind.values()
        for row in rows
        for identifier in row.batch.mapping_manifest
    }
    for mapping in ProductMapping.objects.filter(
        product_id__in=identifiers, product__child_asin=asin
    ).order_by("-effective_from", "-version"):
        mappings[str(mapping.product_id)].append(mapping)
    for kind, rows in rows_by_kind.items():
        parents, refs = defaultdict(set), defaultdict(set)
        for row in rows:
            selected = (
                [row.mapping]
                if row.mapping_id
                else [
                    next(
                        (
                            mapping
                            for mapping in mappings[identifier]
                            if mapping.version <= version and mapping.effective_from <= row.business_date
                        ),
                        None,
                    )
                    for identifier, version in row.batch.mapping_manifest.items()
                ]
            )
            for mapping in selected:
                if mapping is not None:
                    identifier = str(mapping.product_id)
                    parents[identifier].add(mapping.parent_asin)
                    refs[identifier].add(str(row.raw_row_id))
        for identifier, values in parents.items():
            if len(values) > 1:
                result["structure_changes"].append(
                    {
                        "source_kind": kind,
                        "product_id": identifier,
                        "parent_asins": sorted(values),
                        "raw_row_ids": sorted(refs[identifier]),
                    }
                )
    if result["operations"] or result["structure_changes"]:
        result["status"] = "limited"
        result["limitations"].append(
            "窗口重叠运营事件或父子结构变化，基线可比性受限。保留数值信号，不声称已经消除影响，也不认定其为异常原因。"
        )
    return result
