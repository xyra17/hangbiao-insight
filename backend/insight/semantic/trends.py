"""Whole-corpus review trends, counted once per review and topic."""
from datetime import timedelta

from scipy.stats import fisher_exact

from insight.metrics.facts import complete_range, coverage_days, latest_facts
from insight.metrics.statistics import newcombe


def bh_adjust(values):
    result = [1.0] * len(values)
    best = 1.0
    for rank, index in reversed(list(enumerate(sorted(range(len(values)), key=values.__getitem__), 1))):
        best = min(best, values[index] * len(values) / rank)
        result[index] = best
    return result


def topic_trends(run, as_of):
    if not run:
        return {"status": "missing", "topics": [], "windows": []}
    rows = list(latest_facts(run.source_manifest, "reviews", run.child_asin, run.product_id).filter(is_deleted=False, business_date__lt=as_of, business_date__gte=as_of-timedelta(days=112)))
    valid = set(run.review_results.filter(status="completed").values_list("review_id", flat=True))
    coverage = coverage_days(run.source_manifest, "reviews", run.child_asin)
    def windows(days):
        results = []
        for start, end in [(as_of-timedelta(days=2*days), as_of-timedelta(days=days+1)), (as_of-timedelta(days=days), as_of-timedelta(days=1))]:
            collected = {r.pk for r in rows if start <= r.business_date <= end}
            analyzed = collected & valid
            results.append({"start": str(start), "end": str(end), "collected": len(collected), "analyzed": len(analyzed),
                            "coverage": len(analyzed)/len(collected) if collected else None,
                            "collection_complete": complete_range(coverage, start, end), "ids": analyzed})
        return results
    win = windows(28)
    days = 28
    if any(w["analyzed"] < 30 for w in win):
        days, win = 56, windows(56)
    baseline, current = win
    gate = all(w["analyzed"] >= 30 and (w["coverage"] or 0) >= .9 and w["collection_complete"] for w in win)
    claims = list(run.claims.filter(prediction_status="accepted", review_id__in=baseline["ids"] | current["ids"]))
    topics = sorted({(c.primary_topic, c.secondary_topic) for c in claims})
    results, ps = [], []
    for primary, secondary in topics:
        negatives = {c.review_id for c in claims if (c.primary_topic, c.secondary_topic) == (primary, secondary) and c.polarity in {"negative", "mixed"}}
        b, a = len(negatives & baseline["ids"]), len(negatives & current["ids"])
        bn, an = baseline["analyzed"], current["analyzed"]
        p = float(fisher_exact([[a, an-a], [b, bn-b]]).pvalue) if an and bn else 1.0
        interval = newcombe(a, an, b, bn) if an and bn else None
        gap = a/an - b/bn if an and bn else None
        results.append({"primary": primary, "topic": secondary, "baseline_negative": b, "current_negative": a,
                        "baseline_total": bn, "current_total": an, "difference": gap, "difference_interval_95": interval,
                        "p": p, "triggered": bool(gate and a >= 5 and gap is not None and gap >= .1)})
        ps.append(p)
    for item, q in zip(results, bh_adjust(ps)):
        item["q"] = q
        item["statistical_difference"] = bool(gate and q <= .05 and item["difference_interval_95"] and (item["difference_interval_95"][0] > 0 or item["difference_interval_95"][1] < 0))
    for window in win:
        del window["ids"]
    return {"status": "ready" if gate else "insufficient", "days": days, "windows": win, "topics": results,
            "limitations": ["分母为完整窗口内通过校验的独立评论；不是订单缺陷率，也不证明因果。"]}
