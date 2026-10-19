"""Independent CSV answers for immutable initial package snapshots (no app imports)."""
import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from business_package_oracle import daily_windows, read_rows

ZONE = ZoneInfo("America/Los_Angeles")


def stamp(row, field, fallback):
    return datetime.fromisoformat(row[field]) if row.get(field) else datetime.combine(date.fromisoformat(row[fallback]), datetime.min.time(), ZONE)


def verify(output, pack):
    state = json.loads((output / "state.json").read_text())
    samples = json.loads((output / "snapshots-and-oracle.json").read_text())
    inputs = {}
    for path in state["files"]:
        meta = json.loads(Path(path + ".meta.json").read_text())
        inputs[meta["source_payload"]["kind"]] = (read_rows(Path(path)), meta)
    today = date.fromisoformat(state["as_of"])
    answers = []
    for sample in samples:
        snapshot = sample["snapshot"]
        asin = snapshot["child_asin"]
        actual = snapshot["payload"]
        checks = {}
        for kind, fields in [("inventory", ["available", "total", "listing_status"]), ("ratings", ["rating"])]:
            rows, meta = inputs[kind]
            eligible = [r for r in rows if r["child_asin"] == asin and date.fromisoformat(r["business_date"]) <= today]
            chosen = max(eligible, key=lambda r: r["business_date"])
            expected = {field: chosen[field] for field in fields}
            observed = actual["absolute"][kind]
            assert observed["date"] == chosen["business_date"]
            for field in fields:
                if field == "listing_status":
                    assert observed[field] == expected[field]
                else:
                    assert Decimal(str(observed[field])) == Decimal(expected[field])
            checks[kind] = {"expected": expected, "actual": {f: observed[f] for f in fields}, "date": chosen["business_date"]}
        review_rows = inputs.get("reviews", ([], {}))[0]
        chosen_reviews = [r for r in review_rows if r["child_asin"] == asin and r.get("is_deleted", "false").lower() != "true" and today-timedelta(days=28) <= date.fromisoformat(r["business_date"]) < today]
        observed_reviews = actual["absolute"]["sampled_reviews"]
        assert observed_reviews["count"] == len(chosen_reviews)
        if chosen_reviews:
            assert Decimal(observed_reviews["mean_rating"]) == sum(Decimal(r["rating"]) for r in chosen_reviews)/len(chosen_reviews)
        else:
            assert observed_reviews["mean_rating"] is None
        checks["sampled_review_mean"] = {"count": len(chosen_reviews), "mean": observed_reviews["mean_rating"], "unit": "sample arithmetic mean; not page stars"}
        if pack == "04":
            assert all(not meta["upload_payload"]["declared_complete"] for _, meta in inputs.values())
            assert actual["daily"]["status"] == actual["cohort"]["status"] == "missing"
            assert all(actual["occurrence"][k]["status"] == "missing" for k in ["refunds", "returns"])
            checks["coverage"] = "all declarations partial; daily/cohort/occurrence correctly unavailable; absolute snapshots remain observable"
        else:
            daily = daily_windows(inputs["sales_traffic"][0], asin, state["as_of"])
            for window in ["baseline", "current"]:
                for field in ["ordered_units", "order_count", "sessions", "sales_amount", "unit_session_percent"]:
                    assert Decimal(str(actual["daily"][window][field])) == Decimal(daily[window][field])
            checks["daily"] = daily
            orders = [r for r in inputs["orders"][0] if r["child_asin"] == asin and Decimal(r["shipped_quantity"] or "0") > 0]
            groups = defaultdict(list)
            for row in orders:
                groups[row["order_id"]].append(row)
            first = {key: min(stamp(r, "shipped_at", "shipped_date") for r in rows) for key, rows in groups.items()}
            linked = {r["record_id"]: r["order_id"] for r in orders}
            cutoff = today - timedelta(days=31)
            windows = {"baseline": (cutoff-timedelta(days=55), cutoff-timedelta(days=28)), "current": (cutoff-timedelta(days=27), cutoff)}
            for kind in ["refunds", "returns"]:
                rows, meta = inputs[kind]
                rows = [r for r in rows if r["child_asin"] == asin]
                affected = set()
                if kind == "refunds":
                    chains = defaultdict(list)
                    for row in rows:
                        chains[(row["order_line_id"], row["transaction_chain_id"], row["currency"])].append(row)
                    for (line, _, _), chain in chains.items():
                        if line in linked and sum(Decimal(r["amount"]) for r in chain) > 0:
                            order = linked[line]
                            if any(Decimal(r["amount"]) > 0 and first[order] <= stamp(r, "occurred_at", "business_date") < first[order]+timedelta(days=30) for r in chain):
                                affected.add(order)
                else:
                    basis = meta["source_payload"]["return_event"]
                    for row in rows:
                        if row["order_line_id"] in linked and row["status"] in ["received", "completed"]:
                            order = linked[row["order_line_id"]]
                            if first[order] <= stamp(row, basis+"_at", basis+"_date") < first[order]+timedelta(days=30):
                                affected.add(order)
                for window, (start, end) in windows.items():
                    included = {key for key, instant in first.items() if start <= instant.astimezone(ZONE).date() <= end}
                    n, d = len(included & affected), len(included)
                    observed = actual["cohort"][kind][window]
                    assert observed["start"] == str(start) and observed["end"] == str(end)
                    assert observed["numerator"] == n and observed["denominator"] == d
                    assert set(observed["order_ids"]) == included
                    assert Decimal(observed["rate"]) == Decimal(n)/d
                    checks[f"cohort_{kind}_{window}"] = {"numerator": n, "denominator": d, "start": str(start), "end": str(end)}
                for window, (start, end) in {"baseline": (today-timedelta(days=35), today-timedelta(days=8)), "current": (today-timedelta(days=7), today-timedelta(days=1))}.items():
                    chosen = [r for r in rows if start <= date.fromisoformat(r["business_date"]) <= end]
                    observed = actual["occurrence"][kind][window]
                    if kind == "refunds":
                        amounts = defaultdict(Decimal)
                        for row in chosen:
                            amounts[row["currency"]] += Decimal(row["amount"])
                        expected = sum(Decimal(r["amount"]) > 0 for r in chosen)
                        assert observed["reversal_count"] == sum(Decimal(r["amount"]) < 0 for r in chosen)
                        assert {k: Decimal(v) for k, v in observed["amount_by_currency"].items()} == dict(amounts)
                    else:
                        chosen = [r for r in chosen if r["status"] in ["received", "completed"] and r.get("received_date")]
                        expected = len(chosen)
                        assert observed["units"] == sum(int(r["quantity"]) for r in chosen)
                    assert observed["event_count"] == expected
                    checks[f"occurrence_{kind}_{window}"] = {"events": expected, "start": str(start), "end": str(end)}
        answers.append({"asin": asin, "snapshot_id": snapshot["id"], "checks": checks})
    report = {"status": "passed", "pack": pack, "scope": "initial immutable main-package snapshots; independent CSV arithmetic, no application calculators; no lifecycle-current claim", "objects": len(answers), "answers": answers}
    (output / "four-metric-oracle-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pack", choices=["04", "05"], required=True)
    args = parser.parse_args()
    verify(args.output.resolve(), args.pack)
