from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from insight.metrics.parameters import DEFAULTS
from insight.metrics.service import scan_object
from insight.models import BusinessEvent, Job, MetricSnapshot, Notification, RuleConfiguration
from tests_v2.test_metrics import daily_rows, report

pytestmark = pytest.mark.django_db


def complete_preview(response):
    from insight.tasks import run_job

    assert response.status_code == 202, response.data
    job = Job.objects.get(pk=response.data["job_id"])
    run_job(str(job.pk))
    job.refresh_from_db()
    assert job.status == "completed", job.error
    response.data = job.result
    return response


def test_preview_is_read_only_apply_is_versioned_and_recalculates(metric_scope, client):
    users, shops = metric_scope
    shop = shops[0]
    today = timezone.now().astimezone(ZoneInfo(shop.timezone)).date()
    start = today - timedelta(days=35)
    report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    first = scan_object(users["admin"], shop, "B000TEST01", today=today)
    assert any(s["metric"] == "sales_units" for s in first.payload["daily"]["signals"])
    before = (BusinessEvent.objects.count(), MetricSnapshot.objects.count(), Notification.objects.count())
    client.force_authenticate(users["admin"])
    command = {
        "shop": str(shop.pk),
        "child_asin": "B000TEST01",
        "expected_version": 0,
        "values": DEFAULTS | {"sales_change_pct": 99},
    }
    preview = client.post("/api/v1/rule-configurations/preview/", command, format="json")
    complete_preview(preview)
    assert any(s["metric"] == "sales_units" for s in preview.data["before"])
    assert not any(s["metric"] == "sales_units" for s in preview.data["after"])
    assert before == (
        BusinessEvent.objects.count(),
        MetricSnapshot.objects.count(),
        Notification.objects.count(),
    )
    apply = {
        "shop": str(shop.pk),
        "preview_token": preview.data["preview_token"],
        "reason": "验收规则影响预览",
    }
    saved = client.post("/api/v1/rule-configurations/apply/", apply, format="json")
    assert saved.status_code == 200, saved.data
    assert saved.data["version"] == 1
    assert Job.objects.filter(kind="scan_metrics").count() == 4
    assert client.post("/api/v1/rule-configurations/apply/", apply, format="json").status_code == 409
    second = scan_object(users["admin"], shop, "B000TEST01", today=today)
    assert first.pk != second.pk
    assert not any(s["metric"] == "sales_units" for s in second.payload["daily"]["signals"])
    first.refresh_from_db()
    assert first.payload["configuration"]["values"]["sales_change_pct"] == 30


def test_rules_reject_unauthorized_invalid_and_stale_preview(metric_scope, client):
    users, shops = metric_scope
    shop = shops[0]
    command = {"shop": str(shop.pk), "child_asin": "B000TEST01", "expected_version": 0, "values": DEFAULTS}
    client.force_authenticate(users["a"])
    assert client.post("/api/v1/rule-configurations/preview/", command, format="json").status_code == 403
    client.force_authenticate(users["admin"])
    for override in [{"sales_change_pct": 0}, {"observation_days": 1.5}, {"invented": 2}]:
        assert (
            client.post(
                "/api/v1/rule-configurations/preview/",
                command | {"values": DEFAULTS | override},
                format="json",
            ).status_code
            == 400
        )
    preview = client.post("/api/v1/rule-configurations/preview/", command, format="json")
    complete_preview(preview)
    today = timezone.now().astimezone(ZoneInfo(shop.timezone)).date()
    start = today - timedelta(days=35)
    report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    apply = {
        "shop": str(shop.pk),
        "preview_token": preview.data["preview_token"],
        "reason": "已变化的数据不能沿用预览",
    }
    assert client.post("/api/v1/rule-configurations/apply/", apply, format="json").status_code == 409
    assert not RuleConfiguration.objects.exists()


def test_shop_preview_covers_other_asins_and_mapping_change_invalidates_it(metric_scope, client):
    from datetime import date

    from insight.models import Product, ProductMapping

    users, shops = metric_scope
    shop = shops[0]
    product = Product.objects.create(shop=shop, child_asin="B000TEST02", seller_sku="S2", fulfillment="FBM")
    mapping = ProductMapping.objects.create(
        product=product, name="Second", effective_from=date(2025, 1, 1), version=1
    )
    today = timezone.now().astimezone(ZoneInfo(shop.timezone)).date()
    start = today - timedelta(days=35)
    rows = [{**row, "child_asin": asin} for asin in ["B000TEST01", "B000TEST02"] for row in daily_rows(start)]
    report(metric_scope, "sales_traffic", rows, start, today - timedelta(days=1))
    client.force_authenticate(users["admin"])
    command = {
        "shop": str(shop.pk),
        "child_asin": "B000TEST01",
        "expected_version": 0,
        "values": DEFAULTS | {"sales_change_pct": 99},
    }
    response = complete_preview(client.post("/api/v1/rule-configurations/preview/", command, format="json"))
    impact = response.data["impact"]
    assert len(impact) == 4
    second = next(row for row in impact if row["child_asin"] == "B000TEST02" and row["product"] is None)
    assert any(s["metric"] == "sales_units" for s in second["before"])
    assert not any(s["metric"] == "sales_units" for s in second["after"])
    ProductMapping.objects.filter(pk=mapping.pk).update(version=2)
    saved = client.post(
        "/api/v1/rule-configurations/apply/",
        {"shop": str(shop.pk), "preview_token": response.data["preview_token"], "reason": "测试映射改变"},
        format="json",
    )
    assert saved.status_code == 409
    assert not RuleConfiguration.objects.exists()


def test_preview_worker_revocation_hides_result_and_does_not_publish_token(metric_scope, client):
    from insight.models import Membership
    from insight.tasks import run_job

    users, shops = metric_scope
    client.force_authenticate(users["admin"])
    response = client.post(
        "/api/v1/rule-configurations/preview/",
        {"shop": str(shops[0].pk), "child_asin": "B000TEST01", "expected_version": 0, "values": DEFAULTS},
        format="json",
    )
    assert response.status_code == 202
    job = Job.objects.get(pk=response.data["job_id"])
    Membership.objects.filter(user=users["admin"]).update(is_admin=False)
    run_job(str(job.pk))
    job.refresh_from_db()
    assert job.status == "cancelled" and "preview_token" not in job.result
    assert client.get(f"/api/v1/jobs/{job.pk}/").status_code == 404


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_shop_rule_applies_publish_one_version(metric_scope, client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections
    from rest_framework.test import APIClient

    users, shops = metric_scope
    client.force_authenticate(users["admin"])
    preview = complete_preview(
        client.post(
            "/api/v1/rule-configurations/preview/",
            {"shop": str(shops[0].pk), "child_asin": "B000TEST01", "expected_version": 0, "values": DEFAULTS},
            format="json",
        )
    )
    command = {
        "shop": str(shops[0].pk),
        "preview_token": preview.data["preview_token"],
        "reason": "并发保存配置",
    }
    barrier = Barrier(2)

    def save():
        close_old_connections()
        try:
            caller = APIClient()
            caller.force_authenticate(users["admin"])
            barrier.wait(timeout=10)
            return caller.post("/api/v1/rule-configurations/apply/", command, format="json").status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save) for _ in range(2)]
        codes = sorted(f.result(timeout=30) for f in futures)
    assert codes == [200, 409]
    assert RuleConfiguration.objects.count() == 1
    assert Job.objects.filter(kind="scan_metrics").count() == 2
