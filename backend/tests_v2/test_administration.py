import pytest

from insight.models import AuditEntry, Membership, Product, ShopGrant, WorkspaceModelBudgetPolicy

pytestmark = pytest.mark.django_db


def test_administrator_can_register_shop_but_operator_cannot(client, setup_scope):
    users, shops = setup_scope
    workspace = shops[0].workspace
    url = f"/api/v1/workspaces/{workspace.pk}/new-shop/"
    data = {"name": "日本店铺", "marketplace": "JP", "timezone": "Asia/Tokyo", "currency": "JPY"}
    client.force_authenticate(users["a"])
    assert client.post(url, data, format="json").status_code == 403
    client.force_authenticate(users["admin"])
    assert client.post(url, {**data, "timezone": "not/a/timezone"}, format="json").status_code == 400
    assert client.post(url, data, format="json").status_code == 201
    assert client.post(url, data, format="json").status_code == 409


def test_account_creation_hashes_password_and_retains_last_admin(client, setup_scope):
    users, shops = setup_scope
    workspace = shops[0].workspace
    client.force_authenticate(users["admin"])
    response = client.post(
        f"/api/v1/workspaces/{workspace.pk}/new-member/",
        {"username": "new_operator", "password": "test-only-long-secret-value", "is_admin": False},
        format="json",
    )
    assert response.status_code == 201
    assert "password" not in response.json()
    member = Membership.objects.select_related("user").get(pk=response.json()["id"])
    assert member.user.check_password("test-only-long-secret-value")
    assert "test-only-long-secret-value" not in str(list(AuditEntry.objects.values()))
    admin = Membership.objects.get(user=users["admin"])
    result = client.post(
        f"/api/v1/workspaces/{workspace.pk}/member-role/",
        {"member": str(admin.pk), "expected_version": admin.version, "is_admin": False},
        format="json",
    )
    assert result.status_code == 400
    admin.refresh_from_db()
    assert admin.is_admin


def test_grant_revoke_is_scoped_conflict_checked_and_audited(client, setup_scope):
    users, shops = setup_scope
    workspace = shops[0].workspace
    local = Product.objects.create(shop=shops[0], child_asin="B000TEST01", seller_sku="L1", fulfillment="FBA")
    foreign = Product.objects.create(
        shop=shops[1], child_asin="B000TEST01", seller_sku="L1", fulfillment="FBA"
    )
    grant = ShopGrant.objects.get(user=users["a"])
    data = {
        "user": users["a"].pk,
        "shop": str(shops[0].pk),
        "roles": ["operator"],
        "all_products": False,
        "products": [str(local.pk)],
        "expected_version": grant.version,
    }
    url = f"/api/v1/workspaces/{workspace.pk}/set-grant/"
    client.force_authenticate(users["a"])
    assert client.post(url, data, format="json").status_code == 403
    client.force_authenticate(users["admin"])
    assert client.post(url, {**data, "products": [str(foreign.pk)]}, format="json").status_code == 400
    assert client.post(url, data, format="json").status_code == 200
    assert client.post(url, data, format="json").status_code == 409
    client.force_authenticate(users["a"])
    assert client.get("/api/v1/products/").json()["count"] == 1
    client.force_authenticate(users["admin"])
    assert (
        client.post(
            url, {**data, "roles": [], "products": [], "expected_version": 2}, format="json"
        ).status_code
        == 200
    )
    client.force_authenticate(users["a"])
    assert client.get("/api/v1/shops/").json()["count"] == 0
    client.force_authenticate(users["restricted"])
    assert (
        client.get("/api/v1/shops/").json()["count"] == 1
    )  # Other people's revoked grants do not hide this shop.
    assert AuditEntry.objects.filter(action="grant.updated").count() == 2
    client.force_authenticate(users["admin"])
    assert any(row["action"] == "grant.updated" for row in client.get("/api/v1/audit/").json()["results"])


def test_workspace_model_budget_requires_admin_and_explicit_limits(client, setup_scope, monkeypatch):
    users, shops = setup_scope
    workspace = shops[0].workspace
    url = f"/api/v1/workspaces/{workspace.pk}/model-budget/"
    client.force_authenticate(users["a"])
    assert client.get(url).status_code == 403
    client.force_authenticate(users["admin"])
    assert client.get(url).json() == {"configured": False, "external_dispatch": "blocked_until_admin_configuration"}
    payload = {
        "enabled": True, "currency": "USD", "daily_cost_limit": "1.00", "monthly_cost_limit": "20.00",
        "max_concurrent_external": 2, "lease_seconds": 90,
    }
    response = client.put(url, payload, format="json")
    assert response.status_code == 200
    policy = WorkspaceModelBudgetPolicy.objects.get(workspace=workspace)
    assert policy.enabled and policy.daily_cost_limit == 1 and policy.max_concurrent_external == 2
    assert client.get(url).json()["configured"] is True

    from insight.investigation.budget import reserve_workspace_call

    monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "USD")
    reserve_workspace_call(
        workspace_id=workspace.pk, reservation_key="currency-change-guard", provider="controlled",
        operation="test_only", reserved_cost="0.01",
    )
    rejected_currency_change = client.put(
        url,
        {**payload, "currency": "CAD"},
        format="json",
    )
    assert rejected_currency_change.status_code == 400
    policy.refresh_from_db()
    assert policy.currency == "USD"


def test_workspace_execution_policy_is_separate_from_free_run_cost_policy(client, setup_scope):
    from insight.models import WorkspaceExecutionPolicy

    users, shops = setup_scope
    url = f"/api/v1/workspaces/{shops[0].workspace_id}/execution-policy/"
    client.force_authenticate(users["a"])
    assert client.get(url).status_code == 403
    client.force_authenticate(users["admin"])
    before = client.get(url).json()
    assert before["configured"] is False
    assert before["free_deterministic_runs"] == "allowed_with_execution_limit"
    response = client.put(url, {"max_concurrent_investigations": 2, "lease_seconds": 600}, format="json")
    assert response.status_code == 200
    policy = WorkspaceExecutionPolicy.objects.get(workspace=shops[0].workspace)
    assert policy.max_concurrent_investigations == 2 and policy.lease_seconds == 600
    assert response.json()["free_deterministic_runs"] == "allowed_with_execution_limit"
