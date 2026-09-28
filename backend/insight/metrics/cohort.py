from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from zoneinfo import ZoneInfo

from .facts import complete_range, coverage_days, latest_facts
from .parameters import numbers
from .severity import severity_fields
from .statistics import newcombe, wilson


def event_time(row, time_field, date_field, zone):
    timestamp = getattr(row, time_field)
    day = getattr(row, date_field)
    if timestamp:
        return timestamp.astimezone(timezone.utc)
    if day:
        return datetime.combine(day, time.min, ZoneInfo(zone)).astimezone(timezone.utc)
    return None


def calculate_cohort(manifest, asin, today, zone, product_id=None, observation_days=30, rules=None):
    return cohort_from_facts(
        coverage_days(manifest, "orders", asin, required_basis="shipped_date"),
        {kind: coverage_days(manifest, kind, asin) for kind in ("refunds", "returns")},
        list(latest_facts(manifest, "orders", asin, product_id).filter(is_exchange=False)),
        list(latest_facts(manifest, "refunds", asin, product_id)),
        list(latest_facts(manifest, "returns", asin, product_id).select_related("batch__contract")),
        today,
        zone,
        observation_days,
        rules,
    )


def cohort_from_facts(
    order_coverage, event_coverage, orders, refunds, returns, today, zone, observation_days=30, rules=None
):
    """Compute from original lines, so parent totals deduplicate orders before counting."""
    p = numbers(rules)
    if rules is not None:
        observation_days = int(p["observation_days"])
    result = {
        "status": "missing",
        "signals": [],
        "observation_days": observation_days,
        "limitations": ["仅有日期时以站点当日 00:00 为时间精度约定。", "退款订单率不等于缺陷件数率。"],
    }
    if "ready" not in order_coverage.values():
        result["reason"] = "成熟队列需要明确覆盖发货日期的订单来源；下单日期覆盖不能保证发货分母完整。"
        return result
    now = datetime.combine(today, time.min, ZoneInfo(zone)).astimezone(timezone.utc)
    result["status"] = "ready"
    for kind, rows in [("refunds", refunds), ("returns", returns)]:
        days = event_coverage[kind]
        ready = [day for day, status in days.items() if status == "ready" and day < today]
        if not ready:
            result[kind] = {"status": "missing", "reason": "缺少完整的事件来源覆盖。"}
            continue
        data_end = max(ready)
        as_of = min(
            now,
            datetime.combine(data_end + timedelta(days=1), time.min, ZoneInfo(zone)).astimezone(timezone.utc),
        )
        # Use the last fully observed shipment day, never partially matured orders from that day.
        cutoff = min(
            today - timedelta(days=observation_days + 1), data_end - timedelta(days=observation_days)
        )
        baseline_start, current_start = cutoff - timedelta(days=55), cutoff - timedelta(days=27)
        status = "stale" if (today - data_end).days > 2 else "ready"
        if not complete_range(order_coverage, baseline_start, cutoff) or not complete_range(
            days, baseline_start, data_end
        ):
            status = "partial"
        groups = defaultdict(list)
        for row in orders:
            if (
                row.shipped_quantity is None
                and row.shipped_date
                and baseline_start <= row.shipped_date <= cutoff
            ):
                status = "partial"
            if row.shipped_quantity is None or row.shipped_quantity == 0:
                continue
            shipped = event_time(row, "shipped_at", "shipped_date", zone)
            if shipped:
                groups[row.order_id].append((row, shipped))
        linked = {
            row.record_id: (order_id, shipped)
            for order_id, entries in groups.items()
            for row, shipped in entries
        }
        affected, late, unmatched, unclassified_statuses, raw_ids = set(), set(), 0, 0, []
        refund_chains = []
        if kind == "refunds":
            chains = defaultdict(list)
            for row in rows:
                timestamp = event_time(row, "occurred_at", "business_date", zone)
                if timestamp < as_of:
                    chains[(row.order_line_id, row.transaction_chain_id, row.currency)].append(
                        (row, timestamp)
                    )
            for (line, _, currency), entries in chains.items():
                references = [str(row.raw_row_id) for row, _ in entries]
                raw_ids.extend(references)
                net_amount = sum(row.amount for row, _ in entries)
                chain_evidence = {
                    "currency": currency,
                    "net_amount": str(net_amount),
                    "raw_row_ids": references,
                    "classification": "unmatched" if line not in linked else "reversed",
                }
                refund_chains.append(chain_evidence)
                if line not in linked:
                    unmatched += len(entries)
                    continue
                order_id = linked[line][0]
                first = min(moment for _, moment in groups[order_id])
                if net_amount <= 0:
                    continue
                positive_times = [moment for row, moment in entries if row.amount > 0]
                if any(
                    first <= moment < first + timedelta(days=observation_days) for moment in positive_times
                ):
                    affected.add(order_id)
                    chain_evidence["classification"] = "within_observation"
                elif any(moment >= first + timedelta(days=observation_days) for moment in positive_times):
                    late.add(order_id)
                    chain_evidence["classification"] = "late"
                else:
                    chain_evidence["classification"] = "before_shipment"
        else:
            conventions = {row.batch.contract.return_event for row in rows}
            if len(conventions) > 1:
                result[kind] = {"status": "invalid", "reason": "退货申请与仓库收到口径混合，不能比较。"}
                continue
            convention = next(iter(conventions), "received")
            result["return_event"] = convention
            eligible_statuses = {"requested", "received", "completed"} if convention == "requested" else {"received", "completed"}
            for row in rows:
                timestamp = event_time(row, f"{convention}_at", f"{convention}_date", zone)
                if (
                    timestamp is None
                    or timestamp >= as_of
                    or row.status.lower() not in eligible_statuses
                ):
                    if row.status.lower() not in {
                        "requested", "received", "completed", "cancelled", "canceled", "rejected"
                    }:
                        unclassified_statuses += 1
                    continue
                if row.order_line_id not in linked:
                    unmatched += 1
                    continue
                order_id = linked[row.order_line_id][0]
                first = min(moment for _, moment in groups[order_id])
                if first <= timestamp < first + timedelta(days=observation_days):
                    affected.add(order_id)
                elif timestamp >= first + timedelta(days=observation_days):
                    late.add(order_id)
                raw_ids.append(str(row.raw_row_id))

        def count(start, end):
            included, immature, unverified_first = [], 0, 0
            for order_id, entries in groups.items():
                first = min(moment for _, moment in entries)
                day = first.astimezone(ZoneInfo(zone)).date()
                if not start <= day <= end:
                    continue
                purchase_day = min(row.business_date for row, _ in entries)
                if not complete_range(order_coverage, purchase_day, day):
                    unverified_first += 1
                    continue
                last_instant = first + timedelta(days=observation_days)
                last_day = (last_instant - timedelta(microseconds=1)).astimezone(ZoneInfo(zone)).date()
                if last_instant > now or not complete_range(days, day, last_day):
                    immature += 1
                else:
                    included.append(order_id)
            numerator = len(set(included) & affected)
            denominator = len(included)
            return {
                "start": str(start),
                "end": str(end),
                "numerator": numerator,
                "denominator": denominator,
                "rate": str(Decimal(numerator) / denominator) if denominator else None,
                "rate_status": "ready" if denominator else "not_applicable",
                "wilson_95": wilson(numerator, denominator),
                "immature_orders": immature,
                "unverified_first_shipment_orders": unverified_first,
                "order_ids": included,
                "order_evidence": [
                    {
                        "first_shipped_at": min(moment for _, moment in groups[order_id]).isoformat(),
                        "affected": order_id in affected,
                        "raw_row_ids": [str(row.raw_row_id) for row, _ in groups[order_id]],
                    }
                    for order_id in included
                ],
            }

        baseline = count(baseline_start, current_start - timedelta(days=1))
        current = count(current_start, cutoff)
        interval = newcombe(
            current["numerator"], current["denominator"], baseline["numerator"], baseline["denominator"]
        )
        if (
            unmatched
            or unclassified_statuses
            or baseline["unverified_first_shipment_orders"]
            or current["unverified_first_shipment_orders"]
        ):
            status = "partial"
        result[kind] = {
            "status": status,
            "cutoff": str(cutoff),
            "baseline": baseline,
            "current": current,
            "difference_interval_95": interval,
            "late_order_count": len(late),
            "unmatched_records": unmatched,
            "unclassified_status_records": unclassified_statuses,
            "raw_row_ids": raw_ids,
            "refund_chains": refund_chains,
            "observing_orders": sum(
                min(moment for _, moment in entries)
                < as_of
                < min(moment for _, moment in entries) + timedelta(days=observation_days)
                for entries in groups.values()
            ),
        }
        if min(current["denominator"], baseline["denominator"]) < p["cohort_min_orders"]:
            result[kind]["observation"] = {
                "status": "low_sample",
                "affected_orders": current["numerator"],
                "mature_orders": current["denominator"],
                "message": "样本不足：可核查已发生的退款或退货，但不能判断整体风险已经上升。",
            }
            result[kind]["reason"] = "样本不足，仅描述已观察订单，不触发成熟队列风险事件。"
        if (
            status == "ready"
            and min(current["denominator"], baseline["denominator"]) >= p["cohort_min_orders"]
            and current["numerator"] >= p["cohort_min_affected"]
            and interval
            and interval[0] > 0
            and Decimal(current["rate"]) - Decimal(baseline["rate"]) >= (p["cohort_increase_pp"] / 100)
        ):
            result["signals"].append(
                {
                    "metric": f"cohort_{kind}",
                    "family": "experience",
                    "priority": 1,
                    "title": "成熟队列退款订单率上升" if kind == "refunds" else "成熟队列退货订单率上升",
                    **severity_fields(
                        (
                            Fraction(current["numerator"], current["denominator"])
                            - Fraction(baseline["numerator"], baseline["denominator"])
                        )
                        * 100
                        / Fraction(p["cohort_increase_pp"])
                    ),
                    "impact": str(current["numerator"]),
                    "cutoff": str(cutoff),
                }
            )
    return result
