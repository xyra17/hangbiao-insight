from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from insight.ingestion.contracts import digest
from insight.ingestion.service import require_source_access
from insight.models import (
    DailyScanSchedule,
    Job,
    MetricSnapshot,
    Outbox,
    ParentSnapshot,
    Product,
    ProductMapping,
)
from insight.services import audit


def enqueue_objects(actor, shop, asins, trigger):
    require_source_access(actor, shop)
    products = list(Product.objects.filter(shop=shop, child_asin__in=asins))
    objects = [(asin, None) for asin in sorted(set(asins))]
    objects += [(product.child_asin, str(product.pk)) for product in products]
    for asin, product_id in objects:
        job, created = Job.objects.get_or_create(
            shop=shop,
            actor=actor,
            kind="scan_metrics",
            idempotency_key=digest({"trigger": trigger, "asin": asin, "product": product_id}),
            defaults={"payload": {"child_asin": asin, "product_id": product_id}},
        )
        if created:
            Outbox.objects.create(job=job)


def enqueue_missed_windows(schedule, due_date):
    """Recover dated windows without replaying obsolete alerts into live events."""
    if schedule.last_run_date is None:
        return 0
    workspace_zone = ZoneInfo(schedule.shop.workspace.timezone)
    shop_zone = ZoneInfo(schedule.shop.timezone)
    start = schedule.last_run_date + timedelta(days=1)
    end = due_date - timedelta(days=1)
    if start > end:
        return 0
    # Scheduling follows the workspace clock; calculations follow the marketplace.
    first_day = datetime.combine(start, schedule.at, workspace_zone).astimezone(shop_zone).date()
    last_day = datetime.combine(end, schedule.at, workspace_zone).astimezone(shop_zone).date()
    products = list(Product.objects.filter(shop=schedule.shop).order_by("pk"))
    objects = [(asin, None) for asin in sorted({p.child_asin for p in products})]
    objects += [(p.child_asin, str(p.pk)) for p in products]
    count = 0
    for asin, product_id in objects:
        cursor = first_day
        while cursor <= last_day:
            chunk_end = min(cursor + timedelta(days=89), last_day)
            payload = {
                "child_asin": asin,
                "product_id": product_id,
                "from_date": str(cursor),
                "to_date": str(chunk_end),
                "recovery_reason": "missed_daily_schedule",
                "schedule_id": str(schedule.pk),
                "scheduled_workspace_dates": [str(start), str(end)],
            }
            job, created = Job.objects.get_or_create(
                shop=schedule.shop,
                actor=schedule.actor,
                kind="recompute_metrics_range",
                idempotency_key=digest({"trigger": "daily-catchup", **payload}),
                defaults={"payload": payload},
            )
            if created:
                Outbox.objects.create(job=job)
                count += 1
            cursor = chunk_end + timedelta(days=1)
    return count


def schedule_after_import(actor, batch, affected_asins=None):
    source = batch.contract.source
    if not source.is_primary:
        return
    asins = affected_asins or batch.declared_asins
    enqueue_objects(actor, source.shop, asins, f"import:{batch.pk}")
    # Preserve every prior result. Conservatively rebuild existing dated windows
    # for affected objects, including corrections moving records between ASINs.
    today = timezone.now().astimezone(ZoneInfo(source.shop.timezone)).date()
    windows = (
        MetricSnapshot.objects.filter(shop=source.shop, child_asin__in=asins, local_date__lt=today)
        .values_list(
            "child_asin",
            "product_id",
            "local_date",
        )
        .distinct()
    )
    count = 0
    for asin, product_id, local_date in windows.iterator():
        payload = {
            "child_asin": asin,
            "product_id": str(product_id) if product_id else None,
            "as_of": str(local_date),
        }
        job, created = Job.objects.get_or_create(
            shop=source.shop,
            actor=actor,
            kind="recompute_metrics",
            idempotency_key=digest({"trigger": f"import-history:{batch.pk}", **payload}),
            defaults={"payload": payload},
        )
        if created:
            Outbox.objects.create(job=job)
            count += 1
    parents = set(
        ProductMapping.objects.filter(
            product__shop=source.shop,
            product__child_asin__in=asins,
        )
        .exclude(parent_asin="")
        .values_list("parent_asin", flat=True)
    )
    parent_windows = {(parent, None) for parent in parents}
    parent_windows.update(
        ParentSnapshot.objects.filter(
            shop=source.shop,
            parent_asin__in=parents,
            local_date__lt=today,
        )
        .values_list("parent_asin", "local_date")
        .distinct()
    )
    parent_count = 0
    for parent, local_date in sorted(parent_windows, key=lambda item: (item[0], str(item[1]))):
        payload = {"parent_asin": parent}
        if local_date:
            payload["as_of"] = str(local_date)
        job, created = Job.objects.get_or_create(
            shop=source.shop,
            actor=actor,
            kind="summarize_parent",
            idempotency_key=digest({"trigger": f"import-parent:{batch.pk}", **payload}),
            defaults={"payload": payload},
        )
        if created:
            Outbox.objects.create(job=job)
            parent_count += 1
    from insight.derived import enqueue_derived_refresh

    enqueue_derived_refresh(actor, batch, asins)
    audit(
        actor,
        source.shop,
        "import.dependencies_queued",
        batch.pk,
        after={"affected_asins": list(asins), "historical_windows": count, "parent_windows": parent_count},
        request_id=f"import:{batch.pk}",
    )
    DailyScanSchedule.objects.get_or_create(shop=source.shop, defaults={"actor": actor})


@transaction.atomic
def dispatch_daily(schedule_id):
    schedule = (
        DailyScanSchedule.objects.select_for_update(of=("self",))
        .select_related("shop__workspace", "actor")
        .get(pk=schedule_id)
    )
    local = timezone.now().astimezone(ZoneInfo(schedule.shop.workspace.timezone))
    if not schedule.enabled or schedule.last_run_date == local.date() or local.time() < schedule.at:
        return
    try:
        require_source_access(schedule.actor, schedule.shop)
        recovered = enqueue_missed_windows(schedule, local.date())
        enqueue_objects(
            schedule.actor,
            schedule.shop,
            Product.objects.filter(shop=schedule.shop).values_list("child_asin", flat=True).distinct(),
            f"daily:{schedule.pk}:{local.date()}",
        )
    except PermissionDenied:
        schedule.error = "扫描执行身份权限已撤销，请由有权限的管理员重新配置。"
        schedule.save(update_fields=["error"])
        return
    audit(
        schedule.actor,
        schedule.shop,
        "schedule.dispatched",
        schedule.pk,
        before={"last_run_date": str(schedule.last_run_date) if schedule.last_run_date else None},
        after={
            "scheduled_date": str(local.date()),
            "historical_jobs": recovered,
            "dispatched_at": timezone.now().isoformat(),
        },
    )
    schedule.last_run_date, schedule.error = local.date(), ""
    schedule.save(update_fields=["last_run_date", "error"])
