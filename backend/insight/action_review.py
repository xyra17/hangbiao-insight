from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from insight.metrics.cohort import cohort_from_facts
from insight.metrics.daily import window as daily_window
from insight.metrics.facts import (
    authorize_object,
    complete_range,
    coverage_days,
    latest_facts,
    source_manifest,
)
from insight.models import MetricSnapshot, ProductMapping

DAILY_METRICS = {"ordered_units", "sessions", "unit_session_percent"}
COHORT_METRICS = {"refunds", "returns"}
ACTION_REVIEW_VERSION = "action_effect_windows_v1"


def decimal_text(value):
    return format(value.quantize(Decimal("0.000001")).normalize(), "f")


def _snapshot_for_review(task, data, actor, effective_day, review_day):
    authorize_object(actor, task.shop, task.child_asin, task.product_id)
    manifest = source_manifest(task.shop)
    snapshot = (
        MetricSnapshot.objects.filter(
            pk=data.get("snapshot"),
            shop=task.shop,
            child_asin=task.child_asin,
            product_id=task.product_id,
        )
        .exclude(payload__contains={"mode": "historical"})
        .first()
    )
    if not snapshot or snapshot.source_manifest != manifest:
        raise ValidationError("需引用同范围的当前指标快照完成复查。")
    if snapshot.local_date and snapshot.local_date > review_day:
        raise ValidationError("指标快照日期不能晚于实际复查日期。")
    latest = (
        MetricSnapshot.objects.filter(
            shop=task.shop,
            child_asin=task.child_asin,
            product_id=task.product_id,
            source_manifest=manifest,
        )
        .exclude(payload__contains={"mode": "historical"})
        .order_by("-local_date", "-created_at")
        .first()
    )
    if latest is None or latest.pk != snapshot.pk:
        raise ValidationError("需引用该范围最新的当前指标快照完成复查。")
    if not snapshot.local_date or snapshot.local_date < effective_day + timedelta(days=7):
        raise ValidationError("指标快照尚未覆盖动作生效后的七个完整站点日。")

    mappings = ProductMapping.objects.filter(
        product__shop=task.shop,
        product__child_asin=task.child_asin,
        effective_from__lte=snapshot.local_date,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=snapshot.local_date))
    if task.product_id:
        mappings = mappings.filter(product_id=task.product_id)
    mapping_versions = {str(mapping.pk): mapping.version for mapping in mappings}
    if snapshot.payload.get("mapping_versions", {}) != mapping_versions:
        raise ValidationError("指标快照中的商品映射版本已过期，请重新扫描后复查。")
    return snapshot, manifest


def _daily_windows(task, snapshot, manifest, metric, effective_day):
    baseline_start = effective_day - timedelta(days=28)
    baseline_end = effective_day - timedelta(days=1)
    current_start = effective_day + timedelta(days=1)
    current_end = effective_day + timedelta(days=7)
    coverage = coverage_days(manifest, "sales_traffic", task.child_asin)
    if not complete_range(coverage, baseline_start, baseline_end) or not complete_range(
        coverage, current_start, current_end
    ):
        raise ValidationError("动作复查的基线或七日观察窗口存在缺报日期。")

    rows = list(
        latest_facts(manifest, "sales_traffic", task.child_asin, task.product_id)
        .filter(business_date__range=(baseline_start, current_end))
        .select_related("batch__contract")
    )
    desired_grain = "product_day" if task.product_id else "asin_day"
    if any(row.batch.contract.grain != desired_grain for row in rows):
        raise ValidationError("动作复查来源粒度与商品范围不一致，不能分摊或累加 Sessions。")

    baseline = daily_window(rows, baseline_start, baseline_end)
    current = daily_window(rows, current_start, current_end)
    if not baseline["complete"] or not current["complete"]:
        raise ValidationError("动作复查的基线或七日观察窗口缺少逐日记录。")
    field = {
        "ordered_units": "ordered_units",
        "sessions": "sessions",
        "unit_session_percent": "unit_session_percent",
    }[metric]
    if baseline.get(field) is None or current.get(field) is None:
        raise ValidationError("动作复查窗口缺少可比较的指标值。")
    if metric == "unit_session_percent" and (
        baseline.get("ratio_status") != "ready" or current.get("ratio_status") != "ready"
    ):
        raise ValidationError("件数／会话的分母不完整，暂不能形成复查结论。")
    return baseline, current, field


def _cohort_windows(task, snapshot, manifest, metric, effective_day, review_day):
    configuration = snapshot.payload.get("configuration", {}).get("values", {})
    observation_days = int(configuration.get("observation_days", 30))
    baseline_end = effective_day - timedelta(days=observation_days + 1)
    baseline_start = baseline_end - timedelta(days=27)
    current_start = effective_day + timedelta(days=1)
    current_end = current_start + timedelta(days=27)
    if current_end >= review_day:
        raise ValidationError("退款或退货观察队列尚未达到完整的 28 日发货窗口。")

    order_coverage = coverage_days(manifest, "orders", task.child_asin, required_basis="shipped_date")
    event_coverage = {
        kind: coverage_days(manifest, kind, task.child_asin) for kind in ("refunds", "returns")
    }
    orders = list(latest_facts(manifest, "orders", task.child_asin, task.product_id).filter(is_exchange=False))
    refunds = list(latest_facts(manifest, "refunds", task.child_asin, task.product_id))
    returns = list(latest_facts(manifest, "returns", task.child_asin, task.product_id).select_related("batch__contract"))
    windows = {
        "baseline": {"start": baseline_start, "end": baseline_end},
        "current": {"start": current_start, "end": current_end},
    }
    result = cohort_from_facts(
        order_coverage,
        event_coverage,
        orders,
        refunds,
        returns,
        review_day,
        task.shop.timezone,
        observation_days,
        configuration,
        cohort_windows=windows,
    )
    evidence = result.get(metric, {})
    if evidence.get("status") != "ready":
        if evidence.get("baseline", {}).get("immature_orders", 0) or evidence.get("current", {}).get("immature_orders", 0):
            raise ValidationError("退款或退货观察队列尚未完全成熟，暂不能形成复查结论。")
        raise ValidationError("退款或退货的生效日前后队列不完整，暂不能形成复查结论。")
    for name in ("baseline", "current"):
        period = evidence[name]
        if (
            period.get("rate_status") != "ready"
            or period.get("denominator", 0) <= 0
            or period.get("immature_orders", 0)
            or period.get("unverified_first_shipment_orders", 0)
            or period.get("rate") is None
        ):
            raise ValidationError("退款或退货基线与当前窗口没有可比较的成熟订单。")
    return evidence


def validate_review(task, data, actor):
    if task.status != "completed" or not task.effective_at or not task.observed_metrics or not data.get("result"):
        raise ValidationError("复查结论需已完成动作、实际生效日期、观察指标与结果说明。")
    zone = ZoneInfo(task.shop.timezone)
    effective_day = task.effective_at.astimezone(zone).date()
    review_at = data.get("review_at")
    if not review_at:
        raise ValidationError("完成复查必须记录实际复查时间。")
    if review_at > timezone.now():
        raise ValidationError("实际复查时间不能在未来。")
    review_day = review_at.astimezone(zone).date()
    # The effective day is day 0; the daily observation window is days 1..7.
    if review_day < effective_day + timedelta(days=8):
        raise ValidationError("生效日之后至少经过 7 个完整站点日，才能完成指标复查。")
    snapshot, manifest = _snapshot_for_review(task, data, actor, effective_day, review_day)
    comparison = {}
    for metric in task.observed_metrics:
        if metric in DAILY_METRICS:
            baseline, current, field = _daily_windows(task, snapshot, manifest, metric, effective_day)
            baseline_value, current_value = baseline[field], current[field]
            baseline_days, current_days = baseline["days"], current["days"]
            basis = "daily_mean" if metric in {"ordered_units", "sessions"} else "window_value"
            if basis == "daily_mean":
                baseline_total, current_total = Decimal(str(baseline_value)), Decimal(str(current_value))
                baseline_value = decimal_text(baseline_total / baseline_days)
                current_value = decimal_text(current_total / current_days)
                delta = decimal_text(Decimal(current_value) - Decimal(baseline_value))
            else:
                baseline_value, current_value = str(baseline_value), str(current_value)
                delta = decimal_text(Decimal(current_value) - Decimal(baseline_value))
            window_evidence = {
                "version": ACTION_REVIEW_VERSION,
                "effective_date": str(effective_day),
                "baseline": baseline,
                "current": current,
            }
            maturity = {"status": "ready", "immature_orders": 0, "unverified_first_shipment_orders": 0}
        elif metric in COHORT_METRICS:
            evidence = _cohort_windows(task, snapshot, manifest, metric, effective_day, review_day)
            baseline, current = evidence["baseline"], evidence["current"]
            baseline_value, current_value = str(baseline["rate"]), str(current["rate"])
            try:
                delta = str(Decimal(current_value) - Decimal(baseline_value))
            except (InvalidOperation, TypeError, ValueError):
                delta = None
            baseline_days = (date.fromisoformat(baseline["end"]) - date.fromisoformat(baseline["start"])).days + 1
            current_days = (date.fromisoformat(current["end"]) - date.fromisoformat(current["start"])).days + 1
            basis = "mature_cohort_rate"
            window_evidence = {
                "version": ACTION_REVIEW_VERSION,
                "effective_date": str(effective_day),
                "observation_days": snapshot.payload.get("configuration", {}).get("values", {}).get("observation_days", 30),
                "baseline": baseline,
                "current": current,
                "late_order_count": evidence.get("late_order_count", 0),
                "unmatched_records": evidence.get("unmatched_records", 0),
                "unclassified_status_records": evidence.get("unclassified_status_records", 0),
                "raw_row_ids": evidence.get("raw_row_ids", []),
            }
            maturity = {
                "status": "ready",
                "observation_days": window_evidence["observation_days"],
                "immature_orders": current.get("immature_orders", 0),
                "unverified_first_shipment_orders": current.get("unverified_first_shipment_orders", 0),
            }
        else:
            raise ValidationError(f"不支持的动作复查指标：{metric}。")
        comparison[metric] = {
            "baseline": baseline_value,
            "current": current_value,
            "delta": delta,
            "unit": (
                "percent" if metric in {"unit_session_percent", "returns", "refunds"}
                else "units/day" if metric == "ordered_units"
                else "sessions/day" if metric == "sessions"
                else "count"
            ),
            "basis": basis,
            "baseline_days": baseline_days,
            "current_days": current_days,
            **(
                {"baseline_total": str(baseline_total), "current_total": str(current_total)}
                if metric in {"ordered_units", "sessions"}
                else {}
            ),
            "window_start": str(current["start"]),
            "window_end": str(current["end"]),
            "snapshot": str(snapshot.pk),
            "source_manifest": snapshot.source_manifest,
            "snapshot_rule_version": snapshot.rule_version,
            "method": ACTION_REVIEW_VERSION,
            "maturity": maturity,
            "window_evidence": window_evidence,
        }
    return comparison
