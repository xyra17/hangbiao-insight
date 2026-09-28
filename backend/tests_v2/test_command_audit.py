from unittest.mock import patch

import pytest

from insight.models import AuditEntry, Job, Membership, MetricSnapshot, Outbox
from insight.services import audit
from insight.tasks import run_job

pytestmark = pytest.mark.django_db


def request_scan(client, users, shops, key="audit-scan"):
    client.force_authenticate(users["admin"])
    return client.post(
        "/api/v1/snapshots/scan/",
        {"shop": str(shops[0].pk), "child_asin": "B000TEST01", "idempotency_key": key},
        format="json",
    )


def test_scan_command_and_execution_have_separate_idempotent_audits(metric_scope, client):
    users, shops = metric_scope
    response = request_scan(client, users, shops)
    assert response.status_code == 202
    job = Job.objects.get(pk=response.data["id"])
    assert request_scan(client, users, shops).data["id"] == str(job.pk)
    run_job(str(job.pk))
    run_job(str(job.pk))
    assert AuditEntry.objects.filter(action="metrics.command_requested", target_id=str(job.pk)).count() == 1
    receipt = AuditEntry.objects.get(action="job.execution_recorded", target_id=str(job.pk))
    assert receipt.after["status"] == "completed" and receipt.after["attempt"] == 1


def test_failed_execution_audit_does_not_commit_success_receipt(metric_scope, client):
    users, shops = metric_scope
    job = Job.objects.get(pk=request_scan(client, users, shops).data["id"])

    def fail_receipt(actor, shop, action, *args, **kwargs):
        if action == "job.execution_recorded":
            raise RuntimeError("audit unavailable")
        return audit(actor, shop, action, *args, **kwargs)

    with patch("insight.tasks.audit", side_effect=fail_receipt):
        with pytest.raises(RuntimeError):
            run_job(str(job.pk))
    job.refresh_from_db()
    assert job.status == "running"
    assert MetricSnapshot.objects.count() == 1
    run_job(str(job.pk))
    job.refresh_from_db()
    assert job.status == "completed"
    assert MetricSnapshot.objects.count() == 1
    assert AuditEntry.objects.filter(action="job.execution_recorded", target_id=str(job.pk)).count() == 1


def test_revoked_execution_records_cancellation_without_business_write(metric_scope, client):
    users, shops = metric_scope
    job = Job.objects.get(pk=request_scan(client, users, shops).data["id"])
    Membership.objects.filter(user=users["admin"]).update(is_admin=False)
    run_job(str(job.pk))
    job.refresh_from_db()
    assert job.status == "cancelled"
    assert (
        AuditEntry.objects.get(action="job.execution_recorded", target_id=str(job.pk)).after["status"]
        == "cancelled"
    )
    assert not MetricSnapshot.objects.exists()


def test_command_audit_failure_rolls_back_job_and_outbox(metric_scope, client):
    users, shops = metric_scope
    with patch("insight.services.audit", side_effect=RuntimeError("audit unavailable")):
        assert request_scan(client, users, shops).status_code == 500
    assert not Job.objects.exists() and not Outbox.objects.exists()
