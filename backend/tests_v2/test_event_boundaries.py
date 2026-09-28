from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier

import pytest
from django.db import close_old_connections

from insight.metrics.service import scan_object
from insight.models import BusinessEvent, EventEvidence, Notification
from tests_v2.test_metrics import daily_rows, report

pytestmark = pytest.mark.django_db


def test_worsening_threshold_is_exact_and_compares_last_notification(metric_scope):
    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    source, _ = report(metric_scope, "sales_traffic", daily_rows(start, 4), start, today - timedelta(days=1))
    scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    event = BusinessEvent.objects.get()
    initial = event.notified_magnitude
    report(
        metric_scope, "sales_traffic", daily_rows(start, 1), start, today - timedelta(days=1), existing=source
    )
    scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    event.refresh_from_db()
    assert event.magnitude > initial
    assert event.notified_magnitude == initial
    assert not Notification.objects.filter(kind="worsened").exists()
    report(
        metric_scope, "sales_traffic", daily_rows(start, 0), start, today - timedelta(days=1), existing=source
    )
    scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    event.refresh_from_db()
    assert event.notified_magnitude == event.magnitude
    notices = Notification.objects.filter(kind="worsened")
    assert notices.exists()
    assert not notices.filter(user=users["b"]).exists()
    count = notices.count()
    scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    assert notices.count() == count


@pytest.mark.django_db(transaction=True)
def test_concurrent_scans_create_one_event_and_one_notice_per_recipient(metric_scope):
    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    barrier = Barrier(2)

    def scan():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return scan_object(users["admin"], shops[0], "B000TEST01", today=today).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(scan) for _ in range(2)]
        snapshots = [job.result(timeout=30) for job in jobs]
    assert snapshots[0] == snapshots[1]
    assert BusinessEvent.objects.count() == 1
    assert EventEvidence.objects.count() == 2
    recipients = list(Notification.objects.values_list("user_id", flat=True))
    assert recipients and len(recipients) == len(set(recipients))
