"""Independent CSV/Decimal oracle. Never imports application calculators."""
import csv
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

SUM_FIELDS = {"sales_traffic": ["ordered_units", "order_count", "sessions", "sales_amount"], "orders": ["quantity"],
              "refunds": ["amount"], "returns": ["quantity"], "inventory": ["available", "total"]}


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def summary(rows, kind):
    values = {field: Decimal(0) for field in SUM_FIELDS.get(kind, [])}
    missing = {field: 0 for field in values}
    currencies = defaultdict(Decimal)
    for row in rows:
        for field in values:
            if row.get(field, "").strip():
                values[field] += Decimal(str(row[field]))
            else:
                missing[field] += 1
        amount = row.get("amount", row.get("sales_amount", ""))
        if amount:
            currencies[row.get("currency", "")] += Decimal(str(amount))
    return {"rows": len(rows), "asins": sorted({r["child_asin"] for r in rows}), "totals": {k: str(v) for k, v in values.items()},
            "missing_numeric_values": missing, "amount_by_currency": {k: str(v) for k, v in sorted(currencies.items())},
            "financial_note": "independent sources are not assumed financially reconcilable; currencies never netted together"}


def daily_windows(rows, asin, as_of):
    cutoff = date.fromisoformat(as_of) - timedelta(days=1)
    ranges = {"baseline": (cutoff - timedelta(days=34), cutoff - timedelta(days=7)), "current": (cutoff - timedelta(days=6), cutoff)}
    result = {}
    for window, (start, end) in ranges.items():
        chosen = [r for r in rows if r["child_asin"] == asin and start <= date.fromisoformat(r["business_date"]) <= end]
        sums = summary(chosen, "sales_traffic")
        total, denominator = Decimal(sums["totals"]["ordered_units"]), Decimal(sums["totals"]["sessions"])
        result[window] = {"start": str(start), "end": str(end), "rows": len(chosen), "days": (end - start).days + 1,
                          **sums["totals"], "unit_session_percent": str(total / denominator * 100) if denominator else None,
                          "unit": "percent", "denominator": str(denominator)}
    return result
