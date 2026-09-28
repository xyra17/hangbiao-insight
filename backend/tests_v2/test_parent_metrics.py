from datetime import date, timedelta
from decimal import Decimal

import pytest

from insight.metrics.parent import summarize_parent
from insight.models import (
    BusinessEvent,
    Job,
    Notification,
    ParentSnapshot,
    Product,
    ProductMapping,
    ShopGrant,
)
from insight.tasks import run_job
from tests_v2.test_metrics import report

pytestmark = pytest.mark.django_db


@pytest.fixture
def parent_data(metric_scope):
    users, shops = metric_scope
    first = Product.objects.get(shop=shops[0])
    first.mappings.update(parent_asin="B000PARENT")
    second = Product.objects.create(
        shop=shops[0], child_asin="B000TEST02", seller_sku="S2", fulfillment="FBA"
    )
    ProductMapping.objects.create(
        product=second,
        name="高退款子体",
        parent_asin="B000PARENT",
        effective_from=date(2025, 1, 1),
        version=1,
        reason="fixture",
    )
    orders, refunds = [], []
    for day, distribution in [(date(2026, 5, 10), [90, 10]), (date(2026, 6, 10), [10, 90])]:
        for index, count in enumerate(distribution):
            asin, sku = [("B000TEST01", "S1"), ("B000TEST02", "S2")][index]
            for number in range(count):
                identifier = f"{day}-{asin}-{number}"
                orders.append(
                    {
                        "business_date": day,
                        "child_asin": asin,
                        "seller_sku": sku,
                        "fulfillment": "FBA",
                        "record_id": identifier,
                        "order_id": identifier,
                        "quantity": 1,
                        "shipped_quantity": 1,
                        "shipped_date": day,
                        "status": "shipped",
                        "is_exchange": "false",
                    }
                )
                if number < count // (10 if index == 0 else 2):
                    refunds.append(
                        {
                            "business_date": day + timedelta(days=1),
                            "child_asin": asin,
                            "record_id": f"refund-{identifier}",
                            "seller_sku": sku,
                            "fulfillment": "FBA",
                            "order_line_id": identifier,
                            "transaction_chain_id": identifier,
                            "amount": "5",
                            "currency": "USD",
                        }
                    )
    order_source, _ = report(
        metric_scope, "orders", orders, date(2026, 4, 1), date(2026, 7, 31), "product_record", "shipped_date"
    )
    report(metric_scope, "refunds", refunds, date(2026, 4, 1), date(2026, 7, 31), "product_record")
    return users, shops, orders, order_source


def test_parent_mix_shows_weight_shift_without_child_deterioration(parent_data):
    users, shops, _, _ = parent_data
    snapshot = summarize_parent(users["a"], shops[0], "B000PARENT", today=date(2026, 8, 1))
    result = snapshot.payload
    assert result["status"] == "ready"
    assert result["cohort"]["refunds"]["baseline"]["numerator"] == 14
    assert result["cohort"]["refunds"]["current"]["numerator"] == 46
    assert result["cohort"]["refunds"]["current"]["denominator"] == 100
    decomposition = result["decomposition"]["refunds"]
    assert decomposition["status"] == "ready"
    assert Decimal(decomposition["composition_pp"]) == 32
    assert Decimal(decomposition["within_pp"]) == 0
    assert not BusinessEvent.objects.exists()
    assert not Notification.objects.exists()
    assert summarize_parent(users["a"], shops[0], "B000PARENT", today=date(2026, 8, 1)).pk == snapshot.pk


def test_parent_deduplicates_cross_child_orders_and_withholds_decomposition(parent_data):
    users, shops, orders, source = parent_data
    for day in ("2026-05-10", "2026-06-10"):
        for row in orders:
            if row["record_id"] in {f"{day}-B000TEST01-0", f"{day}-B000TEST02-0"}:
                row["order_id"] = f"shared-{day}"
    report((users, shops), "orders", orders, date(2026, 4, 1), date(2026, 7, 31), existing=source)
    snapshot = summarize_parent(users["admin"], shops[0], "B000PARENT", today=date(2026, 8, 1))
    metric = snapshot.payload["cohort"]["refunds"]
    assert metric["baseline"]["denominator"] == metric["current"]["denominator"] == 99
    assert metric["baseline"]["numerator"] == 13
    assert metric["current"]["numerator"] == 45
    decomposition = snapshot.payload["decomposition"]["refunds"]
    assert decomposition["status"] == "limited" and "跨子体订单" in decomposition["reason"]
    assert "composition_pp" not in decomposition


def test_parent_mapping_change_preserves_snapshot_and_blocks_spliced_trend(parent_data):
    users, shops, _, _ = parent_data
    before = summarize_parent(users["admin"], shops[0], "B000PARENT", today=date(2026, 8, 1))
    product = Product.objects.get(shop=shops[0], child_asin="B000TEST02")
    ProductMapping.objects.create(
        product=product,
        name="转移到另一父体",
        parent_asin="B000OTHERP",
        effective_from=date(2026, 6, 1),
        version=2,
        reason="fixture",
    )
    product.version = 2
    product.save()
    after = summarize_parent(users["admin"], shops[0], "B000PARENT", today=date(2026, 8, 1))
    assert after.payload["status"] == "limited"
    assert not after.payload["cohort"] and not after.payload["decomposition"]
    assert len(after.payload["children"]) == 2
    before.refresh_from_db()
    assert before.payload["decomposition"]["refunds"]["status"] == "ready"
    assert after.pk != before.pk


def test_parent_api_scope_redaction_and_worker_revocation(parent_data, client):
    users, shops, _, _ = parent_data
    snapshot = summarize_parent(users["admin"], shops[0], "B000PARENT", today=date(2026, 8, 1))
    url = f"/api/v1/parent-snapshots/{snapshot.pk}/"
    client.force_authenticate(users["b"])
    assert client.get(url).status_code == 404
    grant = ShopGrant.objects.get(user=users["restricted"])
    grant.products.add(Product.objects.get(shop=shops[0], child_asin="B000TEST01"))
    client.force_authenticate(users["restricted"])
    assert client.get(url).status_code == 404
    body = {"shop": str(shops[0].pk), "parent_asin": "B000PARENT", "idempotency_key": "parent-test"}
    assert client.post("/api/v1/parent-snapshots/", body, format="json").status_code == 403
    client.force_authenticate(users["a"])
    data = client.get(url).data["payload"]
    assert "order_ids" not in data["cohort"]["refunds"]["current"]
    assert "order_ids" not in data["children"]["B000TEST01"]["refunds"]["current"]
    response = client.post("/api/v1/parent-snapshots/", body, format="json")
    assert response.status_code == 202
    ShopGrant.objects.filter(user=users["a"]).update(roles=[])
    run_job(response.data["id"])
    assert Job.objects.get(pk=response.data["id"]).status == "cancelled"
    assert ParentSnapshot.objects.count() == 1
