from datetime import date

import pytest

from insight.derived import enqueue_derived_refresh, refresh_derived
from insight.metrics.service import scan_object
from insight.models import ActionTask, DerivedRefresh, InvestigationRun, StandardReport
from insight.semantic.report import compose_report
from tests_v2.test_metrics import report

pytestmark = pytest.mark.django_db(transaction=True)


def revision(scope):
    users, shops = scope
    first = scan_object(users["admin"], shops[0], "B000TEST01")
    old, _ = compose_report(users["admin"], shops[0], "B000TEST01", first, None)
    _, batch = report(scope, "sales_traffic", [{"business_date":"2026-09-20", "ordered_units":5, "sessions":10}], date(2026,9,20), date(2026,9,20))
    return old, batch


def test_refresh_report_preserves_original_action_and_is_idempotent(metric_scope):
    users, shops = metric_scope
    old, batch = revision(metric_scope)
    task = ActionTask.objects.create(shop=shops[0], child_asin=old.child_asin, source_report=old, title="旧报告动作", created_by=users["admin"])
    receipt = DerivedRefresh.objects.get(artifact_id=old.pk)
    result = refresh_derived(receipt.job, lambda: None)
    assert result["status"] == "completed"
    new = StandardReport.objects.get(pk=result["replacement_id"])
    assert new.previous_report_id == old.pk and new.input_versions != old.input_versions
    task.refresh_from_db()
    assert task.source_report_id == old.pk
    assert refresh_derived(receipt.job, lambda: None) == result
    enqueue_derived_refresh(users["admin"], batch, [old.child_asin])
    # The new latest report is a different artifact, but duplicate delivery stays idempotent.
    assert StandardReport.objects.count() == 2


def test_refresh_investigation_frozen_and_original_unchanged(metric_scope):
    users, shops = metric_scope
    original = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01", query="调查 B000TEST01 销量异常", status="completed", max_steps=6)
    _, batch = revision(metric_scope)
    receipt = DerivedRefresh.objects.get(artifact_id=original.pk)
    result = refresh_derived(receipt.job, lambda: None)
    assert result["status"] == "completed", result
    new = InvestigationRun.objects.get(pk=result["replacement_id"])
    assert new.previous_run_id == original.pk and new.read_only_refresh
    assert new.input_versions["source_manifest"] and new.steps.count() == 4
    assert new.result["business_contract"]["expected_next_step"] == "repair_daily_source_before_comparison"
    assert not new.steps.filter(tool_name__in=["review_claims", "text_evidence"]).exists()
    original.refresh_from_db()
    assert original.steps.count() == 0 and original.result == {}
    assert refresh_derived(receipt.job, lambda: None) == result
    assert InvestigationRun.objects.count() == 2
    assert str(batch.contract.source_id) in new.input_versions["source_manifest"]


def test_refresh_real_provider_needs_confirmation(metric_scope):
    from insight.models import SemanticRun
    users, shops = metric_scope
    old, _ = revision(metric_scope)
    semantic = SemanticRun.objects.create(shop=shops[0], requested_by=users["admin"], child_asin=old.child_asin, provider="qwen", status="completed")
    old.semantic_run = semantic
    old.save()
    receipt = DerivedRefresh.objects.get(artifact_id=old.pk)
    assert refresh_derived(receipt.job, lambda: None)["status"] == "needs_confirmation"
    assert StandardReport.objects.count() == 1


def test_refresh_permissions_rechecked_on_completed_receipt(metric_scope):
    from rest_framework.exceptions import PermissionDenied

    from insight.models import Membership
    old, _ = revision(metric_scope)
    receipt = DerivedRefresh.objects.get(artifact_id=old.pk)
    refresh_derived(receipt.job, lambda: None)
    Membership.objects.filter(user=receipt.job.actor).delete()
    with pytest.raises(PermissionDenied):
        refresh_derived(receipt.job, lambda: None)


def test_refresh_report_audit_failure_rolls_back_new_artifact(metric_scope, monkeypatch):
    from insight import derived
    old, _ = revision(metric_scope)
    receipt = DerivedRefresh.objects.get(artifact_id=old.pk)
    def broken(*args, **kwargs):
        raise RuntimeError("audit offline")
    monkeypatch.setattr(derived, "audit", broken)
    with pytest.raises(RuntimeError):
        refresh_derived(receipt.job, lambda: None)
    receipt.refresh_from_db()
    assert receipt.replacement_id is None and StandardReport.objects.count() == 1


def test_investigation_refresh_resumes_existing_successor_after_interruption(metric_scope, monkeypatch):
    from insight.investigation import graph
    users, shops = metric_scope
    original = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01", query="调查 B000TEST01 销量异常", status="completed", max_steps=6)
    revision(metric_scope)
    receipt = DerivedRefresh.objects.get(artifact_id=original.pk)
    execute = graph.execute_investigation
    def interrupted(run):
        raise RuntimeError("process interrupted after successor commit")
    monkeypatch.setattr(graph, "execute_investigation", interrupted)
    with pytest.raises(RuntimeError):
        refresh_derived(receipt.job, lambda: None)
    receipt.refresh_from_db()
    successor = receipt.replacement_id
    assert successor and InvestigationRun.objects.count() == 2
    monkeypatch.setattr(graph, "execute_investigation", execute)
    assert refresh_derived(receipt.job, lambda: None)["replacement_id"] == str(successor)
    assert InvestigationRun.objects.count() == 2


def test_pending_dependency_does_not_exhaust_job_retries(metric_scope):
    from django.utils import timezone

    from insight.tasks import run_job
    users, shops = metric_scope
    run = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01", query="退货调查", status="running")
    revision(metric_scope)
    receipt = DerivedRefresh.objects.get(artifact_id=run.pk)
    for _ in range(4):
        receipt.job.next_run_at = timezone.now()
        receipt.job.save(update_fields=["next_run_at"])
        run_job(str(receipt.job_id))
        receipt.job.refresh_from_db()
        assert receipt.job.status == "retry_wait" and receipt.job.attempts == 0
    assert InvestigationRun.objects.count() == 1


def test_refresh_failure_retry_is_scoped_and_audited(metric_scope, client):
    from insight.derived import artifact_refreshes
    from insight.models import AuditEntry
    old, _ = revision(metric_scope)
    receipt = DerivedRefresh.objects.get(artifact_id=old.pk)
    job = receipt.job
    job.status, job.attempts = "failed", 3
    job.save()
    receipt.status, receipt.detail = "failed", "上次刷新中断"
    receipt.save(update_fields=["status", "detail"])
    client.force_authenticate(metric_scope[0]["b"])
    assert client.post(f"/api/v1/jobs/{job.pk}/retry-refresh/").status_code == 404
    client.force_authenticate(metric_scope[0]["admin"])
    response = client.post(f"/api/v1/jobs/{job.pk}/retry-refresh/")
    assert response.status_code == 202
    job.refresh_from_db()
    assert job.status == "queued" and job.attempts == 0
    receipt.refresh_from_db()
    assert receipt.status == "queued" and receipt.detail == ""
    assert artifact_refreshes("report", old.pk)[0]["status"] == "queued"
    assert AuditEntry.objects.filter(action="artifact.refresh_retried", target_id=str(job.pk)).exists()
    from insight.models import WorkspaceExecutionLease
    from insight.tasks import run_job

    run_job(str(job.pk))
    job.refresh_from_db()
    receipt.refresh_from_db()
    assert job.status == "completed" and receipt.status == "completed"
    assert WorkspaceExecutionLease.objects.get(job=job).status == "released"


def test_two_imports_before_refresh_coalesce_same_current_versions(metric_scope):
    users, shops = metric_scope
    original = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01", query="调查 B000TEST01 销量异常", status="completed", max_steps=6)
    old, batch = revision(metric_scope)
    report(metric_scope, "sales_traffic", [{"business_date":"2026-09-20", "ordered_units":9, "sessions":10}], date(2026,9,20), date(2026,9,20), existing=batch.contract.source)
    receipts = list(DerivedRefresh.objects.order_by("created_at"))
    assert len(receipts) == 4
    results = [refresh_derived(item.job, lambda: None) for item in receipts]
    assert all(item["status"] == "completed" for item in results)
    assert StandardReport.objects.count() == 2 and InvestigationRun.objects.count() == 2
    assert len({str(item.replacement_id) for item in DerivedRefresh.objects.filter(artifact_id=old.pk)}) == 1
    assert len({str(item.replacement_id) for item in DerivedRefresh.objects.filter(artifact_id=original.pk)}) == 1


def test_partial_refresh_is_not_reported_as_completed(metric_scope):
    users, shops = metric_scope
    original = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01", query="调查 B000TEST01 销量异常", status="completed", max_steps=3)
    revision(metric_scope)
    receipt = DerivedRefresh.objects.get(artifact_id=original.pk)
    result = refresh_derived(receipt.job, lambda: None)
    assert result["status"] == "needs_confirmation"
    successor = InvestigationRun.objects.get(pk=result["replacement_id"])
    assert successor.status == "partial"


def test_refresh_does_not_inherit_authority_to_write_tasks(metric_scope):
    # Use the published command boundary, not direct model writes.
    from insight.api_errors import Conflict
    from insight.investigation.commands import create_draft_task
    users, shops = metric_scope
    run = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01", query="修订刷新", status="completed", read_only_refresh=True)
    with pytest.raises(Conflict):
        create_draft_task(run, {"logical_action_id":"refresh-write", "title":"核查任务", "question":"核查异常原因", "completion_criteria":"记录核验结论", "evidence_refs":["evidence"]})
