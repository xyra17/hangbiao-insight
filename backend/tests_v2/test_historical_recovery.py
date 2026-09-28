from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from insight.metrics.service import scan_object
from insight.models import Job, MetricSnapshot, Notification, ProductMapping, Source
from insight.tasks import run_job
from tests_v2.test_ingestion_boundaries import import_rows, source_sample

pytestmark = pytest.mark.django_db


def range_job(metric_scope):
    users, shops, source, row = source_sample(metric_scope, "sales_traffic")
    import_rows(users["admin"], source, [row])
    job = Job.objects.create(
        shop=shops[0],
        actor=users["admin"],
        kind="recompute_metrics_range",
        idempotency_key="recovery",
        payload={
            "child_asin": "B000TEST01",
            "product_id": None,
            "from_date": "2026-09-02",
            "to_date": "2026-09-04",
        },
    )
    return job, source


def interrupt_after_first(job):
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("worker interrupted between dates")
        return scan_object(*args, **kwargs)

    with patch("insight.metrics.service.scan_object", side_effect=interrupted):
        run_job(str(job.pk))
    job.refresh_from_db()
    assert job.status == "retry_wait"
    assert job.result["completed_through"] == "2026-09-02"
    assert len(job.result["snapshot_ids"]) == 1
    assert job.payload["input_versions"]
    job.next_run_at = timezone.now() - timedelta(seconds=1)
    job.save(update_fields=["next_run_at"])


def test_range_retry_reuses_committed_snapshots_and_keeps_frozen_inputs(metric_scope):
    job, _ = range_job(metric_scope)
    interrupt_after_first(job)
    original_id = job.result["snapshot_ids"][0]
    pinned = job.payload["input_versions"]
    run_job(str(job.pk))
    job.refresh_from_db()
    assert job.status == "completed", job.error
    assert job.result["snapshot_ids"][0] == original_id
    assert job.result["input_versions"] == pinned
    assert MetricSnapshot.objects.count() == 3
    assert not Notification.objects.exists()


@pytest.mark.parametrize("change", ["source", "mapping"])
def test_range_retry_stops_instead_of_mixing_source_or_mapping_versions(metric_scope, change):
    job, source = range_job(metric_scope)
    interrupt_after_first(job)
    if change == "source":
        Source.objects.filter(pk=source.pk).update(revision=2)
    else:
        ProductMapping.objects.filter(product__shop=job.shop).update(version=2)
    run_job(str(job.pk))
    job.refresh_from_db()
    assert job.status == "failed"
    assert "改变" in job.error
    assert MetricSnapshot.objects.count() == 1
    assert len(job.result["snapshot_ids"]) == 1


def test_import_correction_queues_current_and_prior_parent_windows(metric_scope):
    from insight.metrics.parent import summarize_parent
    from insight.models import ParentSnapshot

    users, shops, source, row = source_sample(metric_scope, "sales_traffic")
    ProductMapping.objects.filter(product__shop=shops[0]).update(parent_asin="B000PARENT")
    import_rows(users["admin"], source, [row])
    old = summarize_parent(users["admin"], shops[0], "B000PARENT", today=date(2026, 9, 2))
    old_payload = old.payload
    import_rows(users["admin"], source, [{**row, "ordered_units": 20}])
    jobs = list(Job.objects.filter(kind="summarize_parent", payload__as_of="2026-09-02"))
    assert len(jobs) == 1
    run_job(str(jobs[0].pk))
    jobs[0].refresh_from_db()
    assert jobs[0].status == "completed", jobs[0].error
    new = ParentSnapshot.objects.get(pk=jobs[0].result["parent_snapshot_id"])
    old.refresh_from_db()
    assert new.pk != old.pk and old.payload == old_payload
    assert new.source_manifest[str(source.pk)]["revision"] == 2
    assert Job.objects.filter(kind="summarize_parent").count() == 3


@pytest.mark.parametrize(
    "offset,width,code",
    [(730, 365, 202), (730, 366, 400), (731, 0, 400), (2, 0, 202), (2, 2, 400), (2, -1, 400)],
)
def test_history_range_date_limits(metric_scope, client, offset, width, code):
    from zoneinfo import ZoneInfo

    users, shops = metric_scope
    today = timezone.now().astimezone(ZoneInfo(shops[0].timezone)).date()
    start = today - timedelta(days=offset)
    client.force_authenticate(users["admin"])
    response = client.post(
        "/api/v1/snapshots/recompute-range/",
        {
            "shop": str(shops[0].pk),
            "child_asin": "B000TEST01",
            "from_date": str(start),
            "to_date": str(start + timedelta(days=width)),
            "idempotency_key": "limits",
        },
        format="json",
    )
    assert response.status_code == code, response.data
    assert Job.objects.filter(kind="recompute_metrics_range").count() == int(code == 202)
