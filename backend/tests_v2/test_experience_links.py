from datetime import date

import pytest

from insight.metrics.service import scan_object
from insight.semantic.diagnosis import build_diagnostic_payload
from insight.semantic.experiences import experience_links
from tests_v2.test_ingestion_boundaries import SAMPLES
from tests_v2.test_metrics import report

pytestmark = pytest.mark.django_db
DAY = date(2026, 9, 1)


def publish(scope, kind, **updates):
    row = {"business_date": DAY, **SAMPLES[kind], **updates}
    if kind in {"orders", "refunds"}:
        row.update(seller_sku="S1", fulfillment="FBA")
    return report(scope, kind, [row], DAY, DAY,
                  grain="product_record" if kind in {"orders", "refunds"} else "asin_record")


def snapshot(scope):
    return scan_object(scope[0]["admin"], scope[1][0], "B000TEST01", today=date(2026, 9, 2))


def test_same_order_does_not_become_three_independent_experiences(metric_scope):
    for kind in ("orders", "returns", "refunds"):
        publish(metric_scope, kind)
    publish(metric_scope, "support", order_line_id="O1-L1")
    publish(metric_scope, "reviews")
    result = experience_links(snapshot(metric_scope), DAY, DAY)
    assert result["linked_order_count"] == 1
    assert len(result["groups"][0]["records"]) == 3
    assert result["source_counts"] == {"returns": 1, "refunds": 1, "support": 1, "reviews": 1}
    assert result["unlinked"][0]["reason"] == "review_has_no_order_link"
    assert result["independence"] == "unknown"
    assert all(r["batch_id"] and r["line"] == 2 for r in result["groups"][0]["records"])


def test_missing_order_and_outside_window_stay_unknown(metric_scope):
    publish(metric_scope, "support", order_line_id="O1-L1")
    result = experience_links(snapshot(metric_scope), DAY, DAY)
    assert result["linked_order_count"] == 0
    assert result["unlinked"][0]["reason"] == "order_not_resolved"
    assert experience_links(snapshot(metric_scope), date(2026, 9, 2), date(2026, 9, 3))["source_counts"]["support"] == 0


def test_later_order_import_does_not_rewrite_frozen_linkage(metric_scope):
    publish(metric_scope, "support", order_line_id="O1-L1")
    old = snapshot(metric_scope)
    publish(metric_scope, "orders")
    assert experience_links(old, DAY, DAY)["linked_order_count"] == 0
    assert experience_links(snapshot(metric_scope), DAY, DAY)["linked_order_count"] == 1


def test_operation_alone_does_not_explain_business_change(metric_scope):
    publish(metric_scope, "operations")
    result = build_diagnostic_payload(snapshot(metric_scope))
    assert any(f["source"] == "operations" for f in result["evidence_facts"])
    assert not any(c["candidate_id"] in {"price", "variant_mix"} for c in result["candidates"])


def test_diagnostic_payload_exposes_linkage_without_counting_unlinked_rows(metric_scope):
    for kind in ("orders", "returns", "refunds"):
        publish(metric_scope, kind)
    publish(metric_scope, "support", order_line_id="O1-L1")
    publish(metric_scope, "reviews")
    result = build_diagnostic_payload(snapshot(metric_scope))
    linkage = [fact for fact in result["evidence_facts"] if fact["source"] == "experience_links"]
    assert len(linkage) == 1
    assert linkage[0]["value"]["linked_order_count"] == 1
    assert linkage[0]["value"]["unlinked_count"] == 1
    assert result["experience_links"]["independence"] == "unknown"
