import pytest
from rest_framework.test import APIClient

from insight.models import AuditEntry, Job, Outbox, Product, ShopGrant
from insight.tasks import run_job

pytestmark = pytest.mark.django_db


def payload(shop, **changes):
    return {
        "shop": str(shop.pk),
        "child_asin": "B000TEST01",
        "seller_sku": "SKU-1",
        "fulfillment": "FBA",
        "name": "样本",
        "parent_asin": "",
        "owner": None,
        "effective_from": "2026-01-01",
        "reason": "初次建立",
        **changes,
    }


def create(client, users, shop):
    client.force_authenticate(users["admin"])
    response = client.post("/api/v1/products/", payload(shop), format="json")
    assert response.status_code == 201, response.data
    return response.data


def test_anonymous_business_api_denied(client):
    assert client.get("/api/v1/products/").status_code == 403
    assert client.get("/api/v1/jobs/").status_code == 403


def test_login_requires_csrf_and_session_rotates(setup_scope):
    client = APIClient(enforce_csrf_checks=True)
    body = {"username": "a", "password": "unit-test-long-password"}
    assert client.post("/api/v1/login/", body).status_code == 403
    token = client.get("/api/v1/session/").data["csrf_token"]
    response = client.post("/api/v1/login/", body, HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 200
    assert response.data["csrf_token"] != token
    assert client.get("/api/v1/shops/").data["count"] == 1
    assert client.post("/api/v1/logout/", HTTP_X_CSRFTOKEN=response.data["csrf_token"]).status_code == 204
    assert not client.get("/api/v1/session/").data["authenticated"]


def test_shop_and_product_scope_apply_to_lists_detail_and_writes(client, setup_scope):
    users, shops = setup_scope
    p = create(client, users, shops[0])
    client.force_authenticate(users["b"])
    assert client.get("/api/v1/products/").data["count"] == 0
    assert client.get(f"/api/v1/products/{p['id']}/").status_code == 404
    assert client.get(f"/api/v1/products/?shop={shops[0].pk}").status_code == 404
    assert client.post("/api/v1/products/", payload(shops[0]), format="json").status_code == 404
    client.force_authenticate(users["a"])
    assert client.get("/api/v1/products/").data["count"] == 1
    assert (
        client.post("/api/v1/products/", payload(shops[0], seller_sku="SKU-2"), format="json").status_code
        == 403
    )
    client.force_authenticate(users["restricted"])
    assert client.get("/api/v1/products/").data["count"] == 0
    grant = ShopGrant.objects.get(user=users["restricted"])
    grant.products.add(Product.objects.get(pk=p["id"]))
    assert client.get("/api/v1/products/").data["count"] == 1


def test_identity_mapping_history_and_optimistic_conflict(client, setup_scope):
    users, shops = setup_scope
    p = create(client, users, shops[0])
    assert client.post("/api/v1/products/", payload(shops[0]), format="json").status_code == 409
    assert client.post("/api/v1/products/", payload(shops[1]), format="json").status_code == 201
    command = {
        "name": "新名称",
        "effective_from": "2026-02-01",
        "reason": "更新父子映射",
        "expected_version": 1,
    }
    url = f"/api/v1/products/{p['id']}/mappings/"
    response = client.post(url, command, format="json")
    assert response.status_code == 200, response.data
    assert response.data["version"] == 2
    assert len(response.data["mappings"]) == 2
    assert Product.objects.get(pk=p["id"]).mappings.get(version=1).effective_to.isoformat() == "2026-02-01"
    assert client.post(url, command, format="json").status_code == 409
    command["expected_version"] = 2
    assert client.post(url, command, format="json").status_code == 400
    assert AuditEntry.objects.filter(action="product.mapping_revised").count() == 1


def test_owner_must_have_scope_and_failed_create_rolls_back(client, setup_scope):
    users, shops = setup_scope
    client.force_authenticate(users["admin"])
    assert (
        client.post("/api/v1/products/", payload(shops[0], owner=users["b"].pk), format="json").status_code
        == 400
    )
    assert Product.objects.count() == 0
    assert AuditEntry.objects.count() == 0


def test_job_duplicate_delivery_and_revocation(client, setup_scope):
    users, shops = setup_scope
    client.force_authenticate(users["admin"])
    body = {"shop": str(shops[0].pk), "idempotency_key": "same-click"}
    a = client.post("/api/v1/jobs/probe/", body, format="json")
    b = client.post("/api/v1/jobs/probe/", body, format="json")
    assert a.status_code == b.status_code == 202
    assert a.data["id"] == b.data["id"]
    assert Outbox.objects.count() == Job.objects.count() == 1
    run_job(a.data["id"])
    run_job(a.data["id"])
    assert Job.objects.get().status == "completed"
    assert AuditEntry.objects.filter(action="job.completed").count() == 1
    body["idempotency_key"] = "revoked"
    next_job = client.post("/api/v1/jobs/probe/", body, format="json").data["id"]
    users["admin"].is_active = False
    users["admin"].save()
    run_job(next_job)
    assert Job.objects.get(pk=next_job).status == "cancelled"
