from datetime import timedelta

from celery import shared_task
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from .access import require_role
from .derived import DependencyPending
from .models import Job, Outbox
from .services import audit


@shared_task
def dispatch_daily_scans():
    from .action_notifications import notify_overdue_reviews
    from .metrics.event_commands import expire_snoozes
    from .proposals import expire_proposals

    expire_snoozes()
    notify_overdue_reviews()
    expire_proposals()
    from .metrics.scheduling import dispatch_daily
    from .models import DailyScanSchedule

    for identifier in DailyScanSchedule.objects.filter(enabled=True).values_list("id", flat=True):
        dispatch_daily(identifier)


@shared_task
def scan_directories():
    from .ingestion.directory import scan_directory
    from .models import DirectorySource

    for identifier in DirectorySource.objects.filter(enabled=True).values_list("id", flat=True):
        try:
            scan_directory(identifier)
        except PermissionDenied:
            DirectorySource.objects.filter(pk=identifier).update(error="目录执行身份权限已撤销。")


@shared_task
def dispatch_outbox():
    # A send followed by a crash may be sent again: job execution must be idempotent.
    with transaction.atomic():
        records = list(
            Outbox.objects.select_for_update(skip_locked=True)
            .filter(
                Q(job__status="queued")
                | Q(job__status="retry_wait", job__next_run_at__lte=timezone.now())
                | Q(job__status="running", job__heartbeat_at__lt=timezone.now() - timedelta(seconds=60))
            )
            .filter(Q(sent_at__isnull=True) | Q(sent_at__lt=timezone.now() - timedelta(seconds=60)))
            .order_by("created_at")[:50]
        )
        for record in records:
            run_job.apply_async(args=[str(record.job_id)], retry=False)
            record.sent_at = timezone.now()
            record.save(update_fields=["sent_at"])


@shared_task
def run_job(job_id):
    import uuid

    from .execution import current_execution

    execution_context = None
    key = uuid.UUID(job_id).int % (2**63 - 1)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s), pg_backend_pid()", [key])
        acquired, backend_pid = cursor.fetchone()
    if not acquired:
        return

    def heartbeat():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            if cursor.fetchone()[0] != backend_pid:
                raise RuntimeError("Job lock connection lost")
        if not Job.objects.filter(pk=job_id, lease_token=job.lease_token).update(heartbeat_at=timezone.now()):
            raise RuntimeError("Job lease superseded")

    try:
        with transaction.atomic():
            job = Job.objects.select_for_update().select_related("actor", "shop__workspace").get(pk=job_id)
            if job.status not in {"queued", "running", "retry_wait"}:
                return
            if job.status == "retry_wait" and job.next_run_at and job.next_run_at > timezone.now():
                return
            if job.attempts >= 3:
                job.status, job.error = "failed", "任务多次中断，请核查后重新提交。"
                job.finished_at = timezone.now()
                job.save(update_fields=["status", "error", "finished_at"])
                return
            job.attempts += 1
            job.lease_token += 1
            job.status, job.heartbeat_at = "running", timezone.now()
            job.save()
        execution_context = current_execution.set((job.pk, job.lease_token, backend_pid, key))
        try:
            require_role(
                job.actor,
                job.shop,
                *(
                    ["operator", "data_admin", "quality", "support"]
                    if job.kind in {"scan_metrics", "summarize_parent", "semantic_run", "investigation_run", "refresh_derived"}
                    else ["data_admin"]
                ),
            )
            if job.kind == "probe":
                with transaction.atomic():
                    heartbeat()
                    job.result = {"message": "后台任务已执行，数据库连接正常。"}
                    audit(job.actor, job.shop, "job.completed", job.pk, after=job.result)
                    job.status = "completed"
                    job.save(update_fields=["status", "result"])
            elif job.kind in {"scan_metrics", "recompute_metrics", "recompute_metrics_range"}:
                from datetime import date

                from .metrics.service import scan_object

                if job.kind in {"recompute_metrics", "recompute_metrics_range"}:
                    from .ingestion.service import require_source_access

                    require_source_access(job.actor, job.shop)
                heartbeat()
                if job.kind == "recompute_metrics_range":
                    from .execution import check_execution
                    from .metrics.service import RULE_VERSION
                    from .metrics.versions import metric_inputs
                    from .models import Shop

                    with transaction.atomic():
                        Shop.objects.select_for_update().get(pk=job.shop_id)
                        check_execution()
                        if "input_versions" not in job.payload:
                            job.payload["input_versions"] = {**metric_inputs(job.shop), "rule": RULE_VERSION}
                            job.save(update_fields=["payload"])
                    snapshots = []
                    start = date.fromisoformat(job.payload["from_date"])
                    end = date.fromisoformat(job.payload["to_date"])
                    cursor = start
                    while cursor <= end:
                        heartbeat()
                        snapshots.append(
                            scan_object(
                                job.actor,
                                job.shop,
                                job.payload["child_asin"],
                                job.payload.get("product_id"),
                                request_id=str(job.pk),
                                today=cursor,
                                record_events=False,
                                expected_inputs=job.payload["input_versions"],
                            )
                        )
                        with transaction.atomic():
                            check_execution()
                            job.result = {
                                "message": "历史区间正在回补。",
                                "snapshot_ids": [str(snapshot.pk) for snapshot in snapshots],
                                "completed_through": str(cursor),
                                "input_versions": job.payload["input_versions"],
                            }
                            job.save(update_fields=["result"])
                        cursor += timedelta(days=1)
                    job.status, job.result = (
                        "completed",
                        {
                            "message": "历史指标范围回补完成。",
                            "input_versions": job.payload["input_versions"],
                            "snapshot_ids": [str(snapshot.pk) for snapshot in snapshots],
                            "from_date": str(start),
                            "to_date": str(end),
                        },
                    )
                else:
                    snapshot = scan_object(
                        job.actor,
                        job.shop,
                        job.payload["child_asin"],
                        job.payload.get("product_id"),
                        request_id=str(job.pk),
                        today=date.fromisoformat(job.payload["as_of"])
                        if job.kind == "recompute_metrics"
                        else None,
                        record_events=job.kind != "recompute_metrics",
                    )
                    job.status, job.result = (
                        "completed",
                        {"message": "指标扫描完成。", "snapshot_id": str(snapshot.pk)},
                    )
            elif job.kind == "refresh_derived":
                from .derived import refresh_derived

                job.result = refresh_derived(job, heartbeat)
                job.status = "completed"
            elif job.kind == "preview_rules":
                from .metrics.configuration import build_shop_preview

                job.result = build_shop_preview(job, heartbeat)
                job.status = "completed"
            elif job.kind == "summarize_parent":
                from datetime import date

                from .metrics.parent import summarize_parent

                heartbeat()
                snapshot = summarize_parent(
                    job.actor, job.shop, job.payload["parent_asin"], request_id=str(job.pk),
                    today=date.fromisoformat(job.payload["as_of"]) if job.payload.get("as_of") else None,
                )
                job.status, job.result = (
                    "completed",
                    {"message": "父体汇总完成。", "parent_snapshot_id": str(snapshot.pk)},
                )
            elif job.kind == "semantic_run":
                from .models import MetricSnapshot
                from .semantic.report import compose_report
                from .semantic.service import run_semantic

                heartbeat()
                snapshot = (
                    MetricSnapshot.objects.filter(
                        shop=job.shop,
                        child_asin=job.payload["child_asin"],
                        product_id=job.payload.get("product_id"),
                    )
                    .exclude(payload__contains={"mode": "historical"})
                    .order_by("-created_at")
                    .first()
                )
                run = run_semantic(
                    job.actor,
                    job.shop,
                    job.payload["child_asin"],
                    job.payload.get("product_id"),
                    provider=job.payload.get("provider", "deterministic"),
                    request_id=str(job.pk),
                    frozen_manifest=snapshot.source_manifest if snapshot else None,
                )
                report, _ = compose_report(
                    job.actor,
                    job.shop,
                    job.payload["child_asin"],
                    snapshot,
                    run,
                    job.payload.get("product_id"),
                    request_id=str(job.pk),
                )
                job.status, job.result = (
                    "completed",
                    {
                        "message": "语义取证与标准报告完成。" if run.status == "completed" else "语义取证已记录为部分完成，请处理缺口。",
                        "semantic_run_id": str(run.pk),
                        "report_id": str(report.pk),
                        "semantic_status": run.status,
                    },
                )
            elif job.kind == "investigation_run":
                from .investigation.graph import execute_investigation
                from .models import InvestigationRun

                heartbeat()
                with transaction.atomic():
                    run = InvestigationRun.objects.select_for_update().get(pk=job.payload["run_id"])
                    cancelled = run.status == "cancelled"
                if cancelled:
                    job.status, job.result = "cancelled", {"message": "调查已在执行前取消。", "investigation_id": str(run.pk), "status": run.status}
                else:
                    run = execute_investigation(run)
                    job.status, job.result = (
                        "completed",
                        {
                            "message": "自主调查完成."
                            if run.status == "completed"
                            else "自主调查需要继续处理。",
                            "investigation_id": str(run.pk),
                            "status": run.status,
                        },
                    )
            elif job.kind == "parse_import":
                from .ingestion.service import parse_batch

                parse_batch(job.payload["batch_id"], job.actor, heartbeat)
                job.status, job.result = "completed", {"message": "报表解析完成，请核对预览。"}
            elif job.kind == "publish_import":
                from .ingestion.service import publish_batch

                heartbeat()
                batch = publish_batch(
                    job.actor,
                    job.payload["batch_id"],
                    allow_partial=job.payload["allow_partial"],
                    confirm_contract=job.payload["confirm_contract"],
                    request_id=str(job.pk),
                )
                job.status, job.result = "completed", {"message": "报表已发布。", "batch_id": str(batch.pk)}
            else:
                job.status, job.error = "failed", "不支持的任务类型。"
        except DependencyPending as exc:
            job.status, job.error = "retry_wait", str(exc)
            job.attempts -= 1
            job.next_run_at = timezone.now() + timedelta(seconds=30)
        except PermissionDenied:
            job.status, job.error = "cancelled", "权限已撤销，任务未执行。"
        except Exception as exc:
            from rest_framework.exceptions import APIException

            if isinstance(exc, APIException):
                job.status, job.error = "failed", str(exc.detail)[:300]
            else:
                job.status = "retry_wait" if job.attempts < 3 else "failed"
                job.error = f"任务异常（{type(exc).__name__}），请检查运行记录。"
                job.next_run_at = timezone.now() + timedelta(seconds=30 * job.attempts)
        heartbeat()
        job.finished_at = timezone.now() if job.status in {"completed", "failed", "cancelled"} else None
        # Commit the receipt and its audit together, under the current execution lease.
        with transaction.atomic():
            from .execution import check_execution

            check_execution()
            updated = Job.objects.filter(pk=job.pk, lease_token=job.lease_token).update(
                status=job.status, result=job.result, error=job.error, attempts=job.attempts,
                next_run_at=job.next_run_at, finished_at=job.finished_at,
            )
            if not updated:
                raise RuntimeError("Job lease superseded")
            audit(job.actor, job.shop, "job.execution_recorded", job.pk,
                  after={"kind": job.kind, "status": job.status, "attempt": job.attempts,
                         "error": job.error, "lease": job.lease_token}, request_id=str(job.pk))

    finally:
        if execution_context is not None:
            current_execution.reset(execution_context)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
