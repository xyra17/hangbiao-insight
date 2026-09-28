from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

from .facts import latest_facts
from .parameters import numbers
from .severity import severity_fields


def calculate_absolute(manifest, asin, today, product_id=None, rules=None):
    p = numbers(rules)
    result = {"signals": [], "inventory": {"status": "missing"}, "ratings": {"status": "missing"}}
    review_start, review_end = today - timedelta(days=28), today - timedelta(days=1)
    reviews = list(
        latest_facts(manifest, "reviews", asin, product_id)
        .filter(is_deleted=False, business_date__range=(review_start, review_end))
        .order_by("business_date", "record_id")
    )
    result["sampled_reviews"] = {
        "status": "sample_only" if reviews else "missing",
        "start": str(review_start),
        "end": str(review_end),
        "count": len(reviews),
        "mean_rating": str(sum(Decimal(row.rating) for row in reviews) / len(reviews)) if reviews else None,
        "raw_row_ids": [str(row.raw_row_id) for row in reviews],
        "limitation": "仅为近 28 个完整站点日内实际采集评论的算术均分；不代表全部评论，不推算 Amazon 页面星级。",
    }

    def snapshots(kind):
        grain = "product_day" if product_id else "asin_day"
        return list(
            latest_facts(manifest, kind, asin, product_id)
            .filter(business_date__lte=today, batch__contract__grain=grain)
            .order_by("-business_date")[:2]
        )

    inventory = snapshots("inventory")
    if inventory:
        current = inventory[0]
        result["inventory"] = {
            "status": "stale" if (today - current.business_date).days > 2 else "ready",
            "date": str(current.business_date),
            "available": current.available,
            "total": current.total,
            "listing_status": current.listing_status,
            "raw_row_id": str(current.raw_row_id),
        }
        if current.available is None:
            result["inventory"]["limitation"] = "没有可售库存字段，不能由总库存推算。"
        if result["inventory"]["status"] == "ready":
            previous = inventory[1] if len(inventory) > 1 else None
            unavailable = current.listing_status in {"inactive", "suppressed", "unavailable"}
            newly_unavailable = unavailable and (
                previous is None or previous.listing_status not in {"inactive", "suppressed", "unavailable"}
            )
            out = (
                previous is not None
                and current.available == 0
                and previous.available is not None
                and previous.available > 0
            )
            if newly_unavailable or out:
                result["signals"].append(
                    {
                        "metric": "availability",
                        "family": "availability",
                        "priority": 0,
                        "title": "商品不可售" if newly_unavailable else "可售库存由正转零",
                        **severity_fields(1),
                        "impact": None,
                        "cutoff": str(current.business_date),
                    }
                )
    ratings = snapshots("ratings")
    if ratings:
        current = ratings[0]
        result["ratings"] = {
            "status": "stale" if (today - current.business_date).days > 2 else "ready",
            "rating": str(current.rating),
            "date": str(current.business_date),
            "raw_row_id": str(current.raw_row_id),
        }
        if len(ratings) > 1:
            previous = ratings[1]
            drop = previous.rating - current.rating
            if (
                result["ratings"]["status"] == "ready"
                and current.business_date - previous.business_date
                <= timedelta(days=int(p["rating_compare_days"]))
                and drop >= p["rating_drop"]
            ):
                result["signals"].append(
                    {
                        "metric": "page_rating",
                        "family": "experience",
                        "priority": 2,
                        "title": "页面星级下降",
                        **severity_fields(Fraction(drop) / Fraction(p["rating_drop"])),
                        "impact": None,
                        "cutoff": str(current.business_date),
                    }
                )
    return result
