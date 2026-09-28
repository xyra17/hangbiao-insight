"""Parent navigation summaries. They never create or suppress child business events."""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from insight.access import products_for, shops_for
from insight.ingestion.contracts import digest
from insight.models import ParentSnapshot, Product, ProductMapping, RuleConfiguration, Shop
from insight.services import audit

from .cohort import cohort_from_facts
from .facts import coverage_days, latest_facts, source_manifest
from .parameters import DEFAULTS
from .service import RULE_VERSION


def parent_products(shop, parent_asin):
    # Include historical members; a changed relationship cannot bypass scope checks.
    asins = ProductMapping.objects.filter(product__shop=shop, parent_asin=parent_asin).values(
        "product__child_asin"
    )
    return Product.objects.filter(shop=shop, child_asin__in=asins)


def authorize_parent(actor, shop, parent_asin, frozen_ids=()):
    if not shops_for(actor).filter(pk=shop.pk).exists():
        raise PermissionDenied("无权访问该店铺。")
    members = parent_products(shop, parent_asin)
    allowed = products_for(actor).filter(shop=shop)
    if not members.exists() and not frozen_ids:
        raise ValidationError("没有该父 ASIN 的商品映射。")
    required = set(members.values_list("id", flat=True)) | set(frozen_ids)
    if {str(value) for value in required} - {str(value) for value in allowed.values_list("id", flat=True)}:
        raise PermissionDenied("父体汇总要求具有全部相关子体及 SKU 的访问权限。")
    return members


def common_coverage(manifest, kind, asins):
    values = [
        coverage_days(manifest, kind, asin, required_basis="shipped_date" if kind == "orders" else None)
        for asin in asins
    ]
    days = set().union(*(set(value) for value in values))
    return {day: "ready" if all(value.get(day) == "ready" for value in values) else "partial" for day in days}


def mix_decomposition(parent, children):
    """Symmetric decomposition is descriptive and only valid for additive denominators."""
    result = {"status": "limited", "reason": "子体窗口、覆盖或样本不足，不能计算结构分解。", "children": []}
    if parent.get("status") != "ready" or any(child.get("status") != "ready" for child in children.values()):
        return result
    windows = ("baseline", "current")
    if any(not child[w]["denominator"] for child in children.values() for w in windows):
        result["reason"] = "有子体在某窗口没有成熟订单；新进入或退出的子体只并列展示，不补造分解值。"
        return result
    seen = set()
    for child in children.values():
        ids = set(child["baseline"]["order_ids"]) | set(child["current"]["order_ids"])
        if seen & ids:
            result["reason"] = "存在跨子体订单；父体已重新去重，子体订单权重不可直接相加，因此不做结构分解。"
            return result
        seen |= ids
        if any(
            (child[w]["start"], child[w]["end"]) != (parent[w]["start"], parent[w]["end"]) for w in windows
        ):
            return result
    if any(
        sum(child[w][key] for child in children.values()) != parent[w][key]
        for w in windows
        for key in ("numerator", "denominator")
    ):
        result["reason"] = "父体去重后的计数与子体相加不一致，不能使用可加权结构分解。"
        return result
    composition, within = Decimal(0), Decimal(0)
    for asin, child in children.items():
        b, c = child["baseline"], child["current"]
        wb = Decimal(b["denominator"]) / parent["baseline"]["denominator"]
        wc = Decimal(c["denominator"]) / parent["current"]["denominator"]
        rb, rc = Decimal(b["rate"]), Decimal(c["rate"])
        shift = (wc - wb) * (rb + rc) / 2
        change = (rc - rb) * (wb + wc) / 2
        composition += shift
        within += change
        result["children"].append(
            {
                "child_asin": asin,
                "baseline_weight": str(wb),
                "current_weight": str(wc),
                "composition_pp": str(shift * 100),
                "within_pp": str(change * 100),
            }
        )
    result.update(
        status="ready",
        reason="按成熟订单分母使用对称分解；只描述算术构成，不证明因果或整体质量恶化。",
        composition_pp=str(composition * 100),
        within_pp=str(within * 100),
        total_change_pp=str((Decimal(parent["current"]["rate"]) - Decimal(parent["baseline"]["rate"])) * 100),
    )
    return result


def mapping_limits(parent_asin, members, rows, cohort, local_date):
    windows = [
        item[window]
        for kind in ("refunds", "returns")
        if (item := cohort.get(kind, {})).get("baseline")
        for window in ("baseline", "current")
    ]
    if not windows:
        return ["缺少完整队列窗口，暂不能确认父子关系的可比性。"]
    start = min(date.fromisoformat(window["start"]) for window in windows)
    by_product = defaultdict(list)
    for mapping in ProductMapping.objects.filter(product__in=members, effective_from__lte=local_date):
        by_product[str(mapping.product_id)].append(mapping)
    limitations = []
    for product in members:
        mappings = sorted(by_product[str(product.pk)], key=lambda m: (m.effective_from, m.version))
        first = [m for m in mappings if m.effective_from <= start]
        relevant = ([first[-1]] if first else []) + [m for m in mappings if m.effective_from > start]
        if (
            not first
            or any(m.parent_asin != parent_asin for m in relevant)
            or any(
                m.effective_to is not None
                and m.effective_to < (relevant[i + 1].effective_from if i + 1 < len(relevant) else local_date)
                for i, m in enumerate(relevant)
            )
        ):
            limitations.append("比较期间有子体加入、退出或父子关系变化；以下仅并列展示子体，不拼接父体趋势。")
            break
    if any(row.mapping_id is None or row.mapping.parent_asin != parent_asin for row in rows):
        limitations.append("订单导入时冻结的父体映射与本次父体不同或缺失；历史关系不按现有配置改写。")
    return limitations


@transaction.atomic
def summarize_parent(actor, shop, parent_asin, today=None, request_id=""):
    shop = Shop.objects.select_for_update().get(pk=shop.pk)
    members = list(authorize_parent(actor, shop, parent_asin))
    asins = sorted({product.child_asin for product in members})
    local_date = today or timezone.now().astimezone(ZoneInfo(shop.timezone)).date()
    manifest = source_manifest(shop)
    mappings = {str(product.pk): product.version for product in members}
    config = RuleConfiguration.objects.filter(shop=shop).order_by("-version").first()
    rules = config.values if config else DEFAULTS
    rule_version = f"{RULE_VERSION}/c{config.version if config else 0}"
    identity = digest(
        {
            "shop": str(shop.pk),
            "parent": parent_asin,
            "mappings": mappings,
            "manifest": manifest,
            "rule": rule_version,
            "date": str(local_date),
        }
    )
    if existing := ParentSnapshot.objects.filter(fingerprint=identity).first():
        return existing
    all_rows = {
        kind: [
            row
            for asin in asins
            for row in latest_facts(manifest, kind, asin).select_related("mapping", "batch__contract")
        ]
        for kind in ("orders", "refunds", "returns")
    }
    all_rows["orders"] = [row for row in all_rows["orders"] if not row.is_exchange]
    coverage = {kind: common_coverage(manifest, kind, asins) for kind in all_rows}

    def calculate(rows):
        return cohort_from_facts(
            coverage["orders"],
            coverage,
            rows["orders"],
            rows["refunds"],
            rows["returns"],
            local_date,
            shop.timezone,
            rules=rules,
        )

    cohort = calculate(all_rows)
    children = {
        asin: calculate(
            {kind: [row for row in rows if row.child_asin == asin] for kind, rows in all_rows.items()}
        )
        for asin in asins
    }
    # Apply the conservative mapping gate to rows in the actual shipment windows.
    windows = [
        cohort[k][w]
        for k in ("refunds", "returns")
        if cohort.get(k, {}).get("baseline")
        for w in ("baseline", "current")
    ]
    relevant_rows = [
        row
        for row in all_rows["orders"]
        if row.shipped_date
        and any(window["start"] <= str(row.shipped_date) <= window["end"] for window in windows)
    ]
    limits = mapping_limits(parent_asin, members, relevant_rows, cohort, local_date)
    decomposition = (
        {
            kind: mix_decomposition(
                cohort.get(kind, {}), {asin: child.get(kind, {}) for asin, child in children.items()}
            )
            for kind in ("refunds", "returns")
        }
        if not limits
        else {}
    )
    # No signals escape this summary: child events remain independently actionable.
    cohort.pop("signals", None)
    for child in children.values():
        child.pop("signals", None)
    snapshot = ParentSnapshot.objects.create(
        shop=shop,
        parent_asin=parent_asin,
        actor=actor,
        fingerprint=identity,
        rule_version=rule_version,
        source_manifest=manifest,
        mapping_versions=mappings,
        local_date=local_date,
        payload={
            "status": "limited" if limits else "ready",
            "limitations": limits
            + [
                "父体订单从明细重新去重；各子体按同一覆盖范围比较。",
                "不累加子 ASIN Sessions，也不由父体汇总创建、合并或关闭子体事件。",
            ],
            "cohort": cohort if not limits else {},
            "children": children,
            "decomposition": decomposition,
            "configuration": {"values": rules},
        },
    )
    audit(
        actor,
        shop,
        "metrics.parent_summarized",
        snapshot.pk,
        after={"fingerprint": identity, "parent_asin": parent_asin},
        request_id=request_id,
    )
    return snapshot
