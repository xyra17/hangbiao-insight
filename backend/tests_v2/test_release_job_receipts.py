import pytest

from insight.models import Job, ShopGrant


@pytest.mark.django_db
def test_retraction_receipt_is_visible_to_current_source_operator_and_hidden_after_revoke(metric_scope, client):
    users, shops = metric_scope
    grant = ShopGrant.objects.get(user=users["a"], shop=shops[0])
    grant.roles = ["data_admin"]
    grant.save(update_fields=["roles"])
    job = Job.objects.create(shop=shops[0], actor=users["a"], kind="revoke_import", idempotency_key="retraction-receipt", payload={"batch_id": "controlled"}, status="completed")
    client.force_authenticate(users["a"])
    assert client.get(f"/api/v1/jobs/{job.pk}/").status_code == 200
    client.force_authenticate(users["b"])
    assert client.get(f"/api/v1/jobs/{job.pk}/").status_code == 404
    client.force_authenticate(users["a"])
    ShopGrant.objects.filter(user=users["a"], shop=shops[0]).delete()
    assert client.get(f"/api/v1/jobs/{job.pk}/").status_code == 404


@pytest.mark.django_db
def test_current_review_snapshot_excludes_retired_sources_with_same_timestamp(metric_scope):
    from datetime import timedelta

    from django.utils import timezone

    from insight.action_review import _snapshot_for_review
    from insight.models import ActionTask, MetricSnapshot, ProductMapping
    users, shops = metric_scope
    day = timezone.now().date()
    task = ActionTask.objects.create(shop=shops[0], child_asin="B000TEST01", created_by=users["a"], title="controlled review")
    versions = {str(m.pk): m.version for m in ProductMapping.objects.filter(product__shop=shops[0])}
    old = MetricSnapshot.objects.create(shop=shops[0], child_asin="B000TEST01", actor=users["a"], fingerprint="old-source-same-time", rule_version="controlled", source_manifest={"retired": {"revision": 1}}, local_date=day, payload={"mapping_versions": versions})
    current = MetricSnapshot.objects.create(shop=shops[0], child_asin="B000TEST01", actor=users["a"], fingerprint="current-source-same-time", rule_version="controlled", source_manifest={}, local_date=day, payload={"mapping_versions": versions})
    MetricSnapshot.objects.filter(pk=current.pk).update(created_at=old.created_at)
    selected, manifest = _snapshot_for_review(task, {"snapshot": str(current.pk)}, users["a"], day-timedelta(days=8), day)
    assert selected.pk == current.pk and manifest == {}
