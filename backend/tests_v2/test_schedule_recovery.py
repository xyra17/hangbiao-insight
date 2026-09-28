from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

import pytest

from insight.metrics.scheduling import dispatch_daily
from insight.models import (
    AuditEntry,
    DailyScanSchedule,
    Job,
    Membership,
    MetricSnapshot,
    Notification,
    Outbox,
)
from insight.tasks import run_job

pytestmark = pytest.mark.django_db


def schedule_case(metric_scope, last=date(2026, 9, 1)):
    users, shops = metric_scope
    workspace = shops[0].workspace
    workspace.timezone = "Asia/Shanghai"
    workspace.save()
    return DailyScanSchedule.objects.create(
        shop=shops[0], actor=users["admin"], at=time(8), last_run_date=last
    )


def test_offline_recovery_uses_site_dates_without_duplicate_alerts(metric_scope):
    schedule = schedule_case(metric_scope)
    with patch(
        "insight.metrics.scheduling.timezone.now", return_value=datetime(2026, 9, 5, 1, tzinfo=timezone.utc)
    ):
        dispatch_daily(schedule.pk)
        dispatch_daily(schedule.pk)
    historical = list(Job.objects.filter(kind="recompute_metrics_range"))
    assert len(historical) == 2  # ASIN and SKU, independently scoped
    assert all(
        job.payload["from_date"] == "2026-09-01" and job.payload["to_date"] == "2026-09-03"
        for job in historical
    )
    assert Job.objects.filter(kind="scan_metrics").count() == 2
    assert Outbox.objects.count() == 4
    assert AuditEntry.objects.filter(action="schedule.dispatched").count() == 1
    for job in historical:
        run_job(str(job.pk))
        job.refresh_from_db()
        assert job.status == "completed", job.error
        assert job.finished_at is not None
        assert len(job.result["snapshot_ids"]) == 3
    assert MetricSnapshot.objects.count() == 6
    assert not Notification.objects.exists()
    assert all(p["mode"] == "historical" for p in MetricSnapshot.objects.values_list("payload", flat=True))


def test_long_outage_is_chunked_without_losing_dates(metric_scope):
    schedule = schedule_case(metric_scope, date(2026, 1, 1))
    with patch(
        "insight.metrics.scheduling.timezone.now", return_value=datetime(2026, 9, 5, 1, tzinfo=timezone.utc)
    ):
        dispatch_daily(schedule.pk)
    windows = sorted(
        (j.payload["from_date"], j.payload["to_date"])
        for j in Job.objects.filter(kind="recompute_metrics_range")
        if j.payload["product_id"] is None
    )
    assert windows[0][0] == "2026-01-01" and windows[-1][1] == "2026-09-03"
    for start, end in windows:
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days < 90
    for previous, following in zip(windows, windows[1:]):
        assert date.fromisoformat(following[0]) == date.fromisoformat(previous[1]) + timedelta(days=1)


def test_revoked_schedule_does_not_advance_or_enqueue_recovery(metric_scope):
    schedule = schedule_case(metric_scope)
    Membership.objects.filter(user=schedule.actor).update(is_admin=False)
    with patch(
        "insight.metrics.scheduling.timezone.now", return_value=datetime(2026, 9, 5, 1, tzinfo=timezone.utc)
    ):
        dispatch_daily(schedule.pk)
    schedule.refresh_from_db()
    assert schedule.last_run_date == date(2026, 9, 1)
    assert "权限" in schedule.error
    assert not Job.objects.exists() and not Outbox.objects.exists()


def test_schedule_audit_failure_rolls_back_all_jobs_and_cursor(metric_scope):
    schedule = schedule_case(metric_scope)
    with (
        patch(
            "insight.metrics.scheduling.timezone.now",
            return_value=datetime(2026, 9, 5, 1, tzinfo=timezone.utc),
        ),
        patch("insight.metrics.scheduling.audit", side_effect=RuntimeError("audit unavailable")),
    ):
        with pytest.raises(RuntimeError):
            dispatch_daily(schedule.pk)
    schedule.refresh_from_db()
    assert schedule.last_run_date == date(2026, 9, 1)
    assert not Job.objects.exists() and not Outbox.objects.exists()


@pytest.mark.parametrize("day,hour", [(date(2026, 3, 8), 10), (date(2026, 11, 1), 9)])
def test_dst_skipped_or_repeated_schedule_hour_runs_once(metric_scope, day, hour):
    schedule = schedule_case(metric_scope, day - timedelta(days=1))
    workspace = schedule.shop.workspace
    workspace.timezone = "America/Los_Angeles"
    workspace.save()
    schedule.at = time(2, 30) if day.month == 3 else time(1, 30)
    schedule.save()
    now = datetime.combine(day, time(hour, 31), timezone.utc)
    with patch("insight.metrics.scheduling.timezone.now", return_value=now):
        dispatch_daily(schedule.pk)
        dispatch_daily(schedule.pk)
    assert Job.objects.filter(kind="scan_metrics").count() == 2
    assert not Job.objects.filter(kind="recompute_metrics_range").exists()


def test_disabled_schedule_reenabled_recovers_dates_once(metric_scope):
    schedule = schedule_case(metric_scope)
    schedule.enabled = False
    schedule.save()
    with patch("insight.metrics.scheduling.timezone.now", return_value=datetime(2026,9,5,1,tzinfo=timezone.utc)):
        dispatch_daily(schedule.pk)
        assert not Job.objects.exists()
        schedule.enabled = True
        schedule.save()
        dispatch_daily(schedule.pk)
        dispatch_daily(schedule.pk)
    assert Job.objects.filter(kind="scan_metrics").count() == 2
    assert Job.objects.filter(kind="recompute_metrics_range").count() == 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_daily_dispatch_has_single_cursor_and_outbox(metric_scope):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections
    schedule = schedule_case(metric_scope, last=None)
    barrier = Barrier(2)
    def dispatch():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            dispatch_daily(schedule.pk)
        finally:
            close_old_connections()
    with patch("insight.metrics.scheduling.timezone.now", return_value=datetime(2026,9,5,1,tzinfo=timezone.utc)):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(dispatch) for _ in range(2)]
            for future in futures:
                future.result(timeout=20)
    assert Job.objects.filter(kind="scan_metrics").count() == Outbox.objects.count() == 2
    assert AuditEntry.objects.filter(action="schedule.dispatched").count() == 1
