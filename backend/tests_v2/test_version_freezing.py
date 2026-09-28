from datetime import date
from uuid import uuid4

import pytest
from rest_framework.exceptions import ValidationError

from insight.models import ActionTask, AuditEntry, MetricSnapshot, Product, SemanticRun, StandardReport
from insight.semantic.report import compose_report

pytestmark = pytest.mark.django_db


def snapshot(users, shop, manifest, product=None):
    return MetricSnapshot.objects.create(
        shop=shop, child_asin="B000TEST01", product=product, actor=users["a"],
        fingerprint=uuid4().hex, rule_version="test-rule-1", source_manifest=manifest,
        payload={"mapping_versions": {"mapping": 1}}, local_date=date(2026, 9, 20),
    )


def test_report_revisions_preserve_inputs_and_action_binding(metric_scope, client):
    users, shops = metric_scope
    shop = shops[0]
    first = snapshot(users, shop, {"source": 1})
    report, created = compose_report(users["a"], shop, first.child_asin, first, None, request_id="report-test")
    assert created and report.report_version == 1
    client.force_authenticate(users["a"])
    response = client.post("/api/v1/action-tasks/", {
        "shop": str(shop.pk), "child_asin": first.child_asin,
        "source_report": str(report.pk), "title": "核查退货来源",
    }, format="json")
    assert response.status_code == 201
    revised = snapshot(users, shop, {"source": 2})
    replacement, created = compose_report(users["a"], shop, first.child_asin, revised, None)
    assert created and replacement.report_version == 2 and replacement.previous_report_id == report.pk
    report.refresh_from_db()
    assert report.input_versions["source_manifest"] == {"source": 1}
    assert ActionTask.objects.get(pk=response.data["id"]).source_report_id == report.pk
    same, created = compose_report(users["a"], shop, first.child_asin, revised, None)
    assert not created and same.pk == replacement.pk
    assert AuditEntry.objects.filter(action="report.published").count() == 2
    assert AuditEntry.objects.get(target_id=str(report.pk)).request_id == "report-test"


def test_report_rejects_mixed_versions_and_audit_failure_rolls_back(metric_scope, monkeypatch):
    from insight.semantic import report as module
    users, shops = metric_scope
    item = snapshot(users, shops[0], {"source": 1})
    semantic = SemanticRun.objects.create(shop=shops[0], child_asin=item.child_asin,
        source_manifest={"source": 2}, requested_by=users["a"], status="completed")
    with pytest.raises(ValidationError):
        compose_report(users["a"], shops[0], item.child_asin, item, semantic)
    def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(module, "audit", unavailable)
    with pytest.raises(RuntimeError):
        compose_report(users["a"], shops[0], item.child_asin, item, None)
    assert not StandardReport.objects.exists()


def test_empty_report_scope_does_not_collide_with_sku_scope(metric_scope):
    users, shops = metric_scope
    product = Product.objects.get(shop=shops[0])
    aggregate, _ = compose_report(users["a"], shops[0], product.child_asin, None, None)
    sku, _ = compose_report(users["a"], shops[0], product.child_asin, None, None, product.pk)
    assert aggregate.pk != sku.pk
    assert aggregate.report_version == sku.report_version == 1


def test_mapping_only_revision_creates_new_snapshot_and_preserves_old(metric_scope):
    from insight.metrics.service import scan_object
    from insight.services import revise_mapping
    users, shops = metric_scope
    product = Product.objects.get(shop=shops[0])
    before = scan_object(users["admin"], shops[0], product.child_asin, today=date(2026,9,20))
    revise_mapping(users["admin"], product.pk, {
        "name":"新名称", "parent_asin":"B000PARENT", "owner":None,
        "effective_from":date(2026,9,10), "reason":"更正映射", "expected_version":1,
    }, "mapping-freeze")
    after = scan_object(users["admin"], shops[0], product.child_asin, today=date(2026,9,20))
    assert before.pk != after.pk
    assert before.payload["mapping_versions"] != after.payload["mapping_versions"]


def test_stale_job_cannot_commit_report_even_before_receipt_update(metric_scope):
    from django.db import connection

    from insight.execution import current_execution
    from insight.models import Job
    users, shops = metric_scope
    job = Job.objects.create(shop=shops[0], actor=users["admin"], kind="semantic_run", idempotency_key="stale-report", lease_token=2)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        pid = cursor.fetchone()[0]
    token = current_execution.set((job.pk, 1, pid, 42))
    try:
        with pytest.raises(RuntimeError, match="lease superseded"):
            compose_report(users["admin"], shops[0], "B000TEST01", None, None)
    finally:
        current_execution.reset(token)
    assert not StandardReport.objects.exists() and not AuditEntry.objects.filter(action="report.published").exists()
