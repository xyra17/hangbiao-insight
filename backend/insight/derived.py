"""Refresh dependency receipts preserve the original artifact and action bindings."""
from django.contrib.auth import get_user_model
from django.db import transaction

from insight.access import require_role
from insight.ingestion.contracts import digest
from insight.metrics.facts import authorize_object
from insight.models import DerivedRefresh, InvestigationRun, Job, Outbox, SemanticRun, Shop, StandardReport
from insight.services import audit


class DependencyPending(Exception):
    """Wait for an earlier investigation without consuming failure retries."""


def enqueue_derived_refresh(actor, batch, asins):
    shop = batch.contract.source.shop
    # Reports: refresh latest published scope only, never each historical version.
    reports = StandardReport.objects.filter(shop=shop, child_asin__in=asins).order_by("child_asin", "product_id", "-report_version")
    latest = {}
    for report in reports:
        latest.setdefault((report.child_asin, report.product_id), report)
    runs = InvestigationRun.objects.filter(shop=shop, child_asin__in=asins, status__in=["queued", "running", "completed", "partial"], revisions__isnull=True)
    artifacts = [("report", report, actor) for report in latest.values()] + [("investigation", run, run.requested_by) for run in runs]
    for kind, artifact, executor in artifacts:
        refresh, created = DerivedRefresh.objects.get_or_create(shop=shop, batch=batch, artifact_type=kind, artifact_id=artifact.pk)
        if not created:
            continue
        job = Job.objects.create(shop=shop, actor=executor, kind="refresh_derived", idempotency_key=digest({"batch":str(batch.pk), "kind":kind, "artifact":str(artifact.pk)}), payload={"prepared_by": actor.pk, "refresh_id":str(refresh.pk), "child_asin":artifact.child_asin, "product_id":str(artifact.product_id) if artifact.product_id else None})
        refresh.job = job
        refresh.save(update_fields=["job"])
        Outbox.objects.create(job=job)
    return len(artifacts)


def refresh_derived(job, heartbeat):
    from insight.execution import check_execution
    from insight.investigation.graph import execute_investigation
    from insight.metrics.service import scan_object
    from insight.semantic.report import compose_report
    from insight.semantic.service import run_semantic

    heartbeat()
    with transaction.atomic():
        Shop.objects.select_for_update().get(pk=job.shop_id)
        check_execution()
        refresh = DerivedRefresh.objects.select_for_update().get(pk=job.payload["refresh_id"], shop=job.shop)
        require_role(job.actor, job.shop, "operator", "data_admin", "quality", "support")
        authorize_object(job.actor, job.shop, job.payload["child_asin"], job.payload.get("product_id"))
        if refresh.status in {"completed", "needs_confirmation"}:
            return {"refresh_id":str(refresh.pk), "replacement_id":str(refresh.replacement_id) if refresh.replacement_id else None, "status":refresh.status}
        if refresh.replacement_id and refresh.artifact_type == "investigation":
            successor = InvestigationRun.objects.get(pk=refresh.replacement_id)
        else:
            snapshot = scan_object(job.actor, job.shop, job.payload["child_asin"], job.payload.get("product_id"), request_id=str(job.pk))
            if refresh.artifact_type == "report":
                previous = StandardReport.objects.select_related("semantic_run").get(pk=refresh.artifact_id)
                latest = StandardReport.objects.filter(shop=job.shop, child_asin=snapshot.child_asin, product_id=snapshot.product_id).order_by("-report_version").first()
                if latest and latest.pk != previous.pk and latest.metric_snapshot_id == snapshot.pk:
                    refresh.replacement_id, refresh.status = latest.pk, "completed"
                    refresh.save(update_fields=["replacement_id", "status"])
                    audit(job.actor, job.shop, "artifact.refresh_reused", refresh.pk, after={"new": str(latest.pk)}, request_id=str(job.pk))
                    return {"refresh_id": str(refresh.pk), "replacement_id": str(latest.pk), "status": "completed"}
                provider = previous.semantic_run.provider if previous.semantic_run_id else "deterministic"
                if provider not in {"deterministic", "test", "disabled"}:
                    refresh.status = "needs_confirmation"
                    refresh.detail = "指标已更新；原报告包含真实模型语义，需按当前模型授权重新取证，不以关键词结果替换。"
                    refresh.save(update_fields=["status", "detail"])
                    audit(job.actor, job.shop, "artifact.refresh_deferred", refresh.pk, after={"reason":refresh.detail}, request_id=str(job.pk))
                    return {"refresh_id":str(refresh.pk), "status":refresh.status, "message":refresh.detail}
                semantic = run_semantic(job.actor, job.shop, snapshot.child_asin, snapshot.product_id, provider=provider, frozen_manifest=snapshot.source_manifest, request_id=str(job.pk))
                report, _ = compose_report(job.actor, job.shop, snapshot.child_asin, snapshot, semantic, snapshot.product_id, request_id=str(job.pk))
                refresh.replacement_id, refresh.status = report.pk, "completed"
                refresh.save(update_fields=["replacement_id", "status"])
                audit(job.actor, job.shop, "artifact.refreshed", refresh.pk, after={"type":"report", "old":str(previous.pk), "new":str(report.pk)}, request_id=str(job.pk))
                return {"refresh_id":str(refresh.pk), "replacement_id":str(report.pk), "status":"completed"}
            previous = InvestigationRun.objects.get(pk=refresh.artifact_id)
            while previous.revisions.exists():
                previous = previous.revisions.order_by("-created_at").first()
            if previous.status in {"queued", "running"}:
                raise DependencyPending("原调查仍在运行，等待完成后刷新。")
            semantic_id = previous.input_versions.get("semantic_run")
            original_semantic = SemanticRun.objects.filter(pk=semantic_id).first() if semantic_id else None
            if (original_semantic and original_semantic.provider not in {"deterministic", "test", "disabled"}) or previous.status == "cancelled":
                refresh.status = "needs_confirmation"
                refresh.detail = "原调查已取消或包含真实模型结果，请重新确认调查与模型授权。"
                refresh.save(update_fields=["status", "detail"])
                audit(job.actor, job.shop, "artifact.refresh_deferred", refresh.pk, after={"reason": refresh.detail}, request_id=str(job.pk))
                return {"refresh_id": str(refresh.pk), "status": refresh.status}
            if previous.pk != refresh.artifact_id and previous.status == "completed" and previous.input_versions.get("metric_snapshot") == str(snapshot.pk):
                refresh.replacement_id, refresh.status = previous.pk, "completed"
                refresh.save(update_fields=["replacement_id", "status"])
                audit(job.actor, job.shop, "artifact.refresh_reused", refresh.pk, after={"new": str(previous.pk)}, request_id=str(job.pk))
                return {"refresh_id": str(refresh.pk), "replacement_id": str(previous.pk), "status": "completed"}
            # A new read-only run gets new frozen evidence. Old run and writes stay unchanged.
            preparer = get_user_model().objects.get(pk=job.payload["prepared_by"])
            semantic = run_semantic(preparer, job.shop, snapshot.child_asin, snapshot.product_id, provider="deterministic", frozen_manifest=snapshot.source_manifest, request_id=str(job.pk))
            successor = InvestigationRun.objects.create(shop=job.shop, requested_by=job.actor, child_asin=previous.child_asin, product_id=previous.product_id, query=previous.query, max_steps=previous.max_steps, budget_units=previous.budget_units, previous_run=previous, read_only_refresh=True, state={"source_manifest": snapshot.source_manifest, "input_versions": {"metric_snapshot": str(snapshot.pk), "semantic_run": str(semantic.pk)}})
            for key in ("request_scope", "search_start", "search_end", "scope_query"):
                if key in previous.state:
                    successor.state[key] = previous.state[key]
            successor.save(update_fields=["state"])
            refresh.replacement_id, refresh.status = successor.pk, "running"
            refresh.save(update_fields=["replacement_id", "status"])
            audit(job.actor, job.shop, "investigation.refresh_created", successor.pk, after={"previous_run":str(previous.pk), "batch":str(refresh.batch_id), "read_only_refresh":True}, request_id=str(job.pk))
    heartbeat()
    successor = execute_investigation(successor)
    with transaction.atomic():
        check_execution()
        authorize_object(job.actor, job.shop, successor.child_asin, successor.product_id)
        refresh = DerivedRefresh.objects.select_for_update().get(pk=refresh.pk)
        refresh.status = "completed" if successor.status == "completed" else "needs_confirmation"
        refresh.detail = successor.stop_reason
        refresh.save(update_fields=["status", "detail"])
        audit(job.actor, job.shop, "artifact.refreshed", refresh.pk, after={"type":"investigation", "new":str(successor.pk), "status":refresh.status}, request_id=str(job.pk))
    return {"refresh_id":str(refresh.pk), "replacement_id":str(successor.pk), "status":refresh.status}


def artifact_refreshes(kind, artifact_id):
    return [{"id":str(item.pk), "status":item.job.status if item.job and item.job.status in {"failed", "cancelled", "retry_wait"} else item.status,
             "detail":item.job.error if item.job and item.job.error else item.detail,
             "replacement_id":str(item.replacement_id) if item.replacement_id else None,
             "job_id":str(item.job_id) if item.job_id else None, "batch_id":str(item.batch_id)} for item in DerivedRefresh.objects.filter(artifact_type=kind, artifact_id=artifact_id).select_related("job").order_by("-created_at")[:20]]
