from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

from .facts import complete_range, coverage_days, latest_facts
from .parameters import numbers
from .severity import severity_fields


def aggregate(rows, field):
    values = [getattr(row, field) for row in rows]
    return sum(values) if values and all(value is not None for value in values) else None


def window(rows, start, end):
    subset = [row for row in rows if start <= row.business_date <= end]
    # At ASIN grain there must be one row per date; SKU data are never summed into ASIN Sessions.
    complete = len(subset) == (end - start).days + 1
    units, sessions = aggregate(subset, "ordered_units"), aggregate(subset, "sessions")
    orders = aggregate(subset, "order_count")
    ratio_status = (
        "missing" if units is None or sessions is None else "not_applicable" if sessions == 0 else "ready"
    )
    amount = aggregate(subset, "sales_amount")
    currencies = {row.currency for row in subset if row.sales_amount is not None}
    if len(currencies) != 1 or "" in currencies:
        amount = None
    return {
        "start": str(start),
        "end": str(end),
        "days": (end - start).days + 1,
        "complete": complete,
        "ordered_units": units,
        "order_count": orders,
        "sessions": sessions,
        "unit_session_percent": str(Decimal(units) / sessions * 100) if ratio_status == "ready" else None,
        "ratio_status": ratio_status,
        "sales_amount": str(amount) if amount is not None else None,
        "currency": next(iter(currencies)) if amount is not None else None,
        "raw_row_ids": [str(row.raw_row_id) for row in subset],
    }


def calculate_daily(manifest, asin, today, product_id=None, rules=None):
    p = numbers(rules)
    coverage = coverage_days(manifest, "sales_traffic", asin)
    available = [day for day, status in coverage.items() if status == "ready" and day < today]
    if not available:
        return {"status": "missing", "reason": "没有可用的完整销售与流量日。", "signals": []}
    cutoff = max(available)
    start, baseline_start, baseline_end = (
        cutoff - timedelta(days=6),
        cutoff - timedelta(days=34),
        cutoff - timedelta(days=7),
    )
    result = {"cutoff": str(cutoff), "status": "ready", "signals": [], "limitations": []}
    if (today - cutoff).days > 2:
        result.update(status="stale", reason="日数据截至时间落后超过两天，暂停普通异常判定。")
    elif not complete_range(coverage, baseline_start, cutoff):
        result.update(status="partial", reason="比较窗口存在缺报或部分数据，暂停普通异常判定。")
    rows = list(
        latest_facts(manifest, "sales_traffic", asin, product_id)
        .filter(business_date__range=(baseline_start, cutoff))
        .select_related("batch__contract")
    )
    desired_grain = "product_day" if product_id else "asin_day"
    if any(row.batch.contract.grain != desired_grain for row in rows):
        result.update(status="invalid", reason="来源粒度与分析对象不一致；不分摊或累加 Sessions。")
        return result
    baseline, current = window(rows, baseline_start, baseline_end), window(rows, start, cutoff)
    result.update(baseline=baseline, current=current)
    if not baseline["complete"] or not current["complete"]:
        result.update(status="partial", reason="缺少对象对应的逐日记录；缺行保持未知。")
    if result["status"] != "ready":
        return result

    def signal(metric, title, priority, family, magnitude, impact=None):
        result["signals"].append(
            {
                "metric": metric,
                "title": title,
                "priority": priority,
                "family": family,
                **severity_fields(magnitude),
                "impact": impact,
            }
        )

    b_units, c_units = baseline["ordered_units"], current["ordered_units"]
    if b_units is not None and c_units is not None and b_units >= p["sales_min_baseline"]:
        expected = Decimal(b_units) / 4
        change = (Decimal(c_units) - expected) / expected
        result["units_change"] = str(change)
        if (
            abs(change) >= (p["sales_change_pct"] / 100)
            and abs(Decimal(c_units) - expected) >= p["sales_gap"]
        ):
            signal(
                "sales_units",
                "销量下降" if change < 0 else "销量激增",
                1 if change < 0 else 3,
                "sales",
                Fraction(abs(c_units * 4 - b_units), b_units) * 100 / Fraction(p["sales_change_pct"]),
                str(abs(Decimal(c_units) - expected)),
            )
    b_sessions, c_sessions = baseline["sessions"], current["sessions"]
    if b_sessions is not None and c_sessions is not None and b_sessions >= p["traffic_min_baseline"]:
        change = (Decimal(c_sessions) * 4 - b_sessions) / b_sessions
        if change <= (-p["traffic_drop_pct"] / 100):
            signal(
                "sessions",
                "流量下降",
                2,
                "sales",
                Fraction(abs(c_sessions * 4 - b_sessions), b_sessions)
                * 100
                / Fraction(p["traffic_drop_pct"]),
            )
    if (
        b_sessions is not None
        and c_sessions is not None
        and min(b_sessions, c_sessions) >= p["conversion_min_sessions"]
        and baseline["unit_session_percent"] is not None
        and current["unit_session_percent"] is not None
    ):
        b_ratio, c_ratio = Decimal(baseline["unit_session_percent"]), Decimal(current["unit_session_percent"])
        if (
            b_ratio > 0
            and c_ratio <= b_ratio * (1 - p["conversion_drop_pct"] / 100)
            and b_ratio - c_ratio >= p["conversion_drop_pp"]
        ):
            signal(
                "unit_session_percent",
                "件数／会话转化指标下降",
                2,
                "sales",
                (Fraction(b_units * 100, b_sessions) - Fraction(c_units * 100, c_sessions))
                / Fraction(p["conversion_drop_pp"]),
            )
    box = [row.buybox_percent for row in rows if baseline_start <= row.business_date <= baseline_end]
    last = [row.buybox_percent for row in rows if cutoff - timedelta(days=1) <= row.business_date <= cutoff]
    if len(box) == 28 and len(last) == 2 and all(value is not None for value in box + last):
        mean = sum(box) / len(box)
        result["buybox_baseline_daily_mean"] = str(mean)
        if all(value < p["buybox_below_pct"] and mean - value >= p["buybox_drop_pp"] for value in last):
            signal(
                "buybox",
                "购物车占比连续下降",
                2,
                "availability",
                (sum(Fraction(v) for v in box) / len(box) - Fraction(min(last)))
                / Fraction(p["buybox_drop_pp"]),
            )
    result["limitations"].append("转化指标按件数／Sessions 计算，可超过 100%；不使用二项置信区间。")
    return result
