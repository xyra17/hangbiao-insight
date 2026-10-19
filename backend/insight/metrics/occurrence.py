from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

from .facts import complete_range, coverage_days, latest_facts
from .parameters import numbers
from .severity import severity_fields


def calculate_occurrences(manifest, asin, today, product_id=None, rules=None):
    p = numbers(rules)
    result = {"signals": []}
    for kind in ["refunds", "returns"]:
        days = coverage_days(manifest, kind, asin)
        ready = [day for day, status in days.items() if status == "ready" and day < today]
        if not ready:
            result[kind] = {"status": "missing", "reason": "缺少完整发生量来源。"}
            continue
        cutoff = max(ready)
        start, baseline_start = cutoff - timedelta(days=6), cutoff - timedelta(days=34)
        status = "stale" if (today - cutoff).days > 2 else "ready"
        if not complete_range(days, baseline_start, cutoff):
            status = "partial"
        rows = list(
            latest_facts(manifest, kind, asin, product_id)
            .filter(business_date__range=(baseline_start, cutoff))
            .select_related("batch__contract")
        )
        if kind == "returns" and len({row.batch.contract.return_event for row in rows}) > 1:
            result[kind] = {"status": "invalid", "reason": "退货申请与收到口径混用。"}
            continue

        def summarize(window_start, window_end):
            selected = [row for row in rows if window_start <= row.business_date <= window_end]
            if kind == "refunds":
                by_currency = {}
                for row in selected:
                    by_currency[row.currency] = by_currency.get(row.currency, Decimal(0)) + row.amount
                positive = [row for row in selected if row.amount > 0]
                return {
                    "start": str(window_start),
                    "end": str(window_end),
                    "event_count": len(positive),
                    "reversal_count": sum(row.amount < 0 for row in selected),
                    "amount_by_currency": {currency: str(amount) for currency, amount in by_currency.items()},
                    "raw_row_ids": [str(row.raw_row_id) for row in selected],
                }
            # An empty window retains the observed source contract, rather than
            # silently converting a requested-return source into received.
            convention = next((row.batch.contract.return_event for row in selected),
                              next((row.batch.contract.return_event for row in rows), "received"))
            all_raw_row_ids = [str(row.raw_row_id) for row in selected]
            eligible_statuses = {"requested", "received", "completed"} if convention == "requested" else {"received", "completed"}
            unclassified_rows = [row for row in selected if row.status.lower() not in {
                "requested", "received", "completed", "cancelled", "canceled", "rejected"
            }]
            selected = [
                row
                for row in selected
                if getattr(row, f"{convention}_date") is not None
                and row.status.lower() in eligible_statuses
            ]
            return {
                "start": str(window_start),
                "end": str(window_end),
                "event_count": len(selected),
                "units": sum(row.quantity for row in selected),
                "event_basis": convention,
                "unclassified_records": len(unclassified_rows),
                "unclassified_raw_row_ids": [str(row.raw_row_id) for row in unclassified_rows],
                "raw_row_ids": all_raw_row_ids,
            }

        baseline, current = summarize(baseline_start, start - timedelta(days=1)), summarize(start, cutoff)
        result[kind] = {
            "status": status,
            "cutoff": str(cutoff),
            "baseline": baseline,
            "current": current,
            "limitation": "按发生日期核查数量，不除以当期销量，也不解释为缺陷率。",
        }
        expected = Decimal(baseline["event_count"]) / 4
        difference = Decimal(current["event_count"]) - expected
        if (
            status == "ready"
            and difference >= p["occurrence_gap"]
            and (expected == 0 or difference / expected >= (p["occurrence_increase_pct"] / 100))
        ):
            result["signals"].append(
                {
                    "metric": f"occurrence_{kind}",
                    "family": "experience",
                    "priority": 2,
                    "title": "当期退款发生量增加" if kind == "refunds" else "当期退货发生量增加",
                    **severity_fields(Fraction(difference) / Fraction(p["occurrence_gap"])),
                    "impact": str(current["event_count"]),
                    "cutoff": str(cutoff),
                }
            )
    return result
