"""Cross-shop and partial-SKU visibility for every existing derived artifact family."""
from datetime import date
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from insight.models import (
    ActionTask,
    BusinessEvent,
    ChangeProposal,
    InvestigationRun,
    Job,
    Membership,
    MetricSnapshot,
    Notification,
    ObservationLead,
    Product,
    ProductMapping,
    SemanticRun,
    Shop,
    ShopGrant,
    StandardReport,
    Workspace,
)


@pytest.fixture
def setup_scope(db):
    users = {
        name: get_user_model().objects.create_user(username=name, password="unit-test-long-password")
        for name in ["admin", "a", "b", "restricted"]
    }
    workspace = Workspace.objects.create(name="测试工作区", is_test=True)
    for name, user in users.items():
        Membership.objects.create(workspace=workspace, user=user, is_admin=name == "admin")
    shops = [
        Shop.objects.create(
            workspace=workspace, name=name, marketplace="US", timezone="America/Los_Angeles", currency="USD"
        )
        for name in ["A", "B"]
    ]
    for name, shop in [("a", shops[0]), ("b", shops[1])]:
        ShopGrant.objects.create(user=users[name], shop=shop, roles=["operator"])
    ShopGrant.objects.create(user=users["restricted"], shop=shops[0], roles=["operator"], all_products=False)
    return users, shops


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def enable_workspace_model_budget(monkeypatch):
    """Explicit XTS-only budget for controlled provider fixtures."""
    def configure(workspace, actor, *, daily="100", monthly="1000", concurrency=4, lease_seconds=300):
        from insight.models import WorkspaceModelBudgetPolicy

        monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "XTS")
        return WorkspaceModelBudgetPolicy.objects.update_or_create(
            workspace=workspace,
            defaults={
                "enabled": True,
                "currency": "XTS",
                "daily_cost_limit": daily,
                "monthly_cost_limit": monthly,
                "max_concurrent_external": concurrency,
                "lease_seconds": lease_seconds,
                "updated_by": actor,
            },
        )[0]

    return configure


@pytest.fixture
def metric_scope(setup_scope, settings, tmp_path):
    settings.STORAGE_ROOT = tmp_path
    users, shops = setup_scope
    for shop in shops:
        product = Product.objects.create(
            shop=shop, child_asin="B000TEST01", seller_sku="S1", fulfillment="FBA"
        )
        ProductMapping.objects.create(
            product=product, name="测试商品", effective_from=date(2025, 1, 1), version=1, reason="fixture"
        )
    return users, shops


@pytest.fixture
def artifacts(metric_scope):
    users, shops = metric_scope
    product = Product.objects.get(shop=shops[0])
    second = Product.objects.create(shop=shops[0], child_asin=product.child_asin, seller_sku="S2", fulfillment="FBM")
    foreign = Product.objects.get(shop=shops[1])
    grant = ShopGrant.objects.get(user=users["restricted"])
    grant.products.add(product)
    groups = []
    for scope in (product, second, foreign, None):
        shop = scope.shop if scope else shops[0]
        data = {"shop": shop, "product": scope, "child_asin": "B000TEST01"}
        snapshot = MetricSnapshot.objects.create(**data, actor=users["admin"], fingerprint=uuid4().hex, rule_version="scope-test", source_manifest={}, payload={}, local_date=date(2026, 9, 1))
        event = BusinessEvent.objects.create(**data, object_key=str(scope.pk) if scope else "asin", family="sales", title="权限哨兵", priority=2, last_cutoff=date(2026, 9, 1), last_seen=timezone.now())
        run = InvestigationRun.objects.create(**data, requested_by=users["restricted"], query="权限测试", status="completed")
        groups.append({
            "snapshots": snapshot,
            "events": event,
            "observations": ObservationLead.objects.create(**data, object_key=str(scope.pk) if scope else "asin", family="sales", metric="units", title="观察哨兵", snapshot=snapshot, fingerprint=uuid4().hex, detail={}, last_seen=timezone.now()),
            "reports": StandardReport.objects.create(**data, metric_snapshot=snapshot, publication_key=uuid4().hex, payload={"marker": "sensitive-result"}),
            "semantic-runs": SemanticRun.objects.create(**data, requested_by=users["restricted"]),
            "action-tasks": ActionTask.objects.create(**data, event=event, title="内部任务", created_by=users["restricted"]),
            "change-proposals": ChangeProposal.objects.create(**data, operation="test", target={}, requested_by=users["restricted"], idempotency_key=uuid4().hex),
            "investigations": run,
            "notifications": Notification.objects.create(user=users["restricted"], event=event, kind="new", data_version=uuid4().hex),
            "jobs": Job.objects.create(shop=shop, actor=users["restricted"], kind="scan_metrics", idempotency_key=uuid4().hex, payload={"child_asin": data["child_asin"], "product_id": str(scope.pk) if scope else None}, result={"snapshot_id": str(snapshot.pk)}),
        })
    return users, shops, grant, groups

