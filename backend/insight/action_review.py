from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from insight.models import MetricSnapshot


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
    # The effective day is day 0.  A review on day 8 is the earliest point at
    # which site days 1..7 are complete; a timestamp on day 7 is still inside
    # the seventh site day and cannot close the observation.
    if review_day < effective_day + timedelta(days=8):
        raise ValidationError("生效日之后至少经过 7 个完整站点日，才能完成指标复查。")
    snapshot = MetricSnapshot.objects.filter(pk=data.get("snapshot"), shop=task.shop,
        child_asin=task.child_asin, product_id=task.product_id).exclude(payload__contains={"mode": "historical"}).first()
    from insight.metrics.facts import source_manifest

    if not snapshot or snapshot.source_manifest != source_manifest(task.shop):
        raise ValidationError("需引用同范围的当前指标快照完成复查。")
    if snapshot.local_date and snapshot.local_date > review_day:
        raise ValidationError("指标快照日期不能晚于实际复查日期。")
    comparison = {}
    for metric in task.observed_metrics:
        if metric in {"returns", "refunds"}:
            evidence = snapshot.payload.get("cohort", {}).get(metric, {})
        else:
            evidence = snapshot.payload.get("daily", {})
        current = evidence.get("current", {})
        baseline = evidence.get("baseline", {})
        if (
            evidence.get("status") != "ready"
            or not baseline.get("start")
            or not baseline.get("end")
            or not current.get("start")
            or not current.get("end")
        ):
            raise ValidationError("观察指标的数据覆盖或成熟队列不完整，暂不能形成复查结论。")
        try:
            baseline_start = date.fromisoformat(baseline["start"])
            baseline_end = date.fromisoformat(baseline["end"])
            window_start = date.fromisoformat(current["start"])
            window_end = date.fromisoformat(current["end"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("观察窗口日期格式无效，不能形成复查结论。") from exc
        # Every day in the comparison must be after the effective day and
        # strictly before the actual review day.  Seven calendar days are the
        # minimum; this prevents a partial current window from looking like a
        # successful post-action result.
        if (
            baseline_start > baseline_end
            or baseline_end >= effective_day
            or window_start <= effective_day
            or window_end < effective_day + timedelta(days=7)
            or window_end - window_start < timedelta(days=6)
            or window_end >= review_day
        ):
            raise ValidationError("指标窗口必须覆盖生效日后的完整观察期；退货退款需等待对应发货队列成熟。")
        if metric in {"returns", "refunds"}:
            if baseline.get("rate_status") != "ready" or current.get("rate_status") != "ready":
                raise ValidationError("退货或退款基线与当前窗口没有可比较的成熟订单。")
            if baseline.get("immature_orders", 0) or baseline.get("unverified_first_shipment_orders", 0):
                raise ValidationError("退货或退款基线队列尚未完全成熟，暂不能形成复查结论。")
            if current.get("immature_orders", 0) or current.get("unverified_first_shipment_orders", 0):
                raise ValidationError("退货或退款观察队列尚未完全成熟，暂不能形成复查结论。")
        elif baseline.get("complete") is not True or current.get("complete") is not True:
            raise ValidationError("观察窗口存在缺报日期，暂不能形成复查结论。")
        field = {
            "ordered_units": "ordered_units",
            "sessions": "sessions",
            "unit_session_percent": "unit_session_percent",
            "returns": "rate",
            "refunds": "rate",
        }[metric]
        baseline_value, current_value = baseline.get(field), current.get(field)
        if baseline_value is None or current_value is None:
            raise ValidationError("基线或当前窗口缺少可比较的指标值，暂不能形成复查结论。")
        if metric == "unit_session_percent" and (
            baseline.get("ratio_status") != "ready" or current.get("ratio_status") != "ready"
        ):
            raise ValidationError("件数／会话的分母不完整，暂不能形成复查结论。")
        delta = None
        if isinstance(baseline_value, (int, float)) and isinstance(current_value, (int, float)):
            delta = current_value - baseline_value
        elif baseline_value is not None and current_value is not None:
            try:
                delta = str(Decimal(str(current_value)) - Decimal(str(baseline_value)))
            except (InvalidOperation, TypeError, ValueError):
                delta = None
        comparison[metric] = {
            "baseline": baseline_value,
            "current": current_value,
            "delta": delta,
            "unit": "percent" if metric in {"unit_session_percent", "returns", "refunds"} else "count",
            "window_start": current["start"],
            "window_end": current["end"],
            "snapshot": str(snapshot.pk),
            "method": evidence.get("method") or snapshot.rule_version,
            "maturity": {
                "status": "ready",
                "immature_orders": current.get("immature_orders", 0),
                "unverified_first_shipment_orders": current.get("unverified_first_shipment_orders", 0),
            },
        }
    return comparison
