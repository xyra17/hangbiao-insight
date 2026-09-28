from datetime import date, timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from insight.models import (
    ActionExecution,
    ActionTask,
    AuditEntry,
    BusinessEvent,
    EventEvidence,
    MetricSnapshot,
    Notification,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def event_case(metric_scope):
    users, shops = metric_scope
    item = BusinessEvent.objects.create(shop=shops[0], child_asin="B000TEST01", object_key="asin:B000TEST01",
        family="sales", title="销量变化", priority=2, last_cutoff=date(2026, 9, 20), last_seen=timezone.now())
    snapshot = MetricSnapshot.objects.create(shop=shops[0], child_asin=item.child_asin, actor=users["a"],
        fingerprint=uuid4().hex, rule_version="test", source_manifest={}, payload={}, local_date=date(2026, 9, 21))
    EventEvidence.objects.create(event=item, snapshot=snapshot, metric="ordered_units", detail={})
    return users, item, snapshot


def command(client, event, operation, **values):
    event.refresh_from_db()
    return client.post(f"/api/v1/events/{event.pk}/command/", {
        "expected_version": event.version, "operation": operation, "reason": "已核对原始数据", **values,
    }, format="json")


def test_event_claim_observe_close_keeps_action_and_evidence(event_case, client):
    users, event, snapshot = event_case
    client.force_authenticate(users["a"])
    assert command(client, event, "claim").status_code == 200
    assert Notification.objects.filter(event=event, kind="assigned", user=users["a"]).count() == 1
    task = ActionTask.objects.create(shop=event.shop, event=event, child_asin=event.child_asin,
        title="排查", created_by=users["a"], status="completed")
    assert command(client, event, "observe", action_task=str(task.pk)).status_code == 400
    task.effective_at, task.review_at = timezone.now() - timedelta(days=8), timezone.now()
    task.observed_metrics = ["ordered_units"]
    task.save()
    ActionExecution.objects.create(task=task, actor=users["a"], notes="已执行", completed_at=task.effective_at)
    assert command(client, event, "observe", action_task=str(task.pk)).status_code == 200
    assert command(client, event, "close", closure_reason="resolved", evidence_refs=[str(snapshot.pk)]).status_code == 400
    assert command(client, event, "close", closure_reason="resolved", risk_resolved=True, evidence_refs=[str(snapshot.pk)]).status_code == 200
    event.refresh_from_db()
    assert event.status == "closed" and event.closure_evidence == [str(snapshot.pk)]
    assert event.closed_at and task.executions.count() == 1
    assert AuditEntry.objects.filter(target_id=str(event.pk), action__in=["event.claim", "event.observe", "event.close"]).count() == 3


def test_event_scope_owner_version_and_audit_failure(event_case, client, monkeypatch):
    from insight.metrics import event_commands
    users, event, snapshot = event_case
    client.force_authenticate(users["b"])
    assert command(client, event, "claim").status_code == 404
    client.force_authenticate(users["a"])
    assert command(client, event, "snooze", snoozed_until=(timezone.now() + timedelta(days=1)).isoformat()).status_code == 403
    assert command(client, event, "claim", owner=users["b"].pk).status_code == 403
    assert command(client, event, "claim").status_code == 200
    assert command(client, event, "close", expected_version=1, closure_reason="false_positive", evidence_refs=[str(snapshot.pk)]).status_code == 409
    assert command(client, event, "close", closure_reason="false_positive", evidence_refs=[str(uuid4())]).status_code == 400
    def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(event_commands, "audit", unavailable)
    assert command(client, event, "close", closure_reason="false_positive", evidence_refs=[str(snapshot.pk)]).status_code == 500
    event.refresh_from_db()
    assert event.status == "processing" and not event.closed_at


def test_snooze_expiry_preserves_status_and_audits_system_actor(event_case, client):
    from insight.metrics.event_commands import expire_snoozes
    users, event, _ = event_case
    client.force_authenticate(users["a"])
    assert command(client, event, "claim").status_code == 200
    assert command(client, event, "snooze", snoozed_until=(timezone.now() + timedelta(days=1)).isoformat()).status_code == 200
    event.refresh_from_db()
    assert event.status == "processing"
    BusinessEvent.objects.filter(pk=event.pk).update(snoozed_until=timezone.now() - timedelta(seconds=1))
    expire_snoozes()
    expire_snoozes()
    event.refresh_from_db()
    assert event.snoozed_until is None and event.status == "processing"
    assert AuditEntry.objects.filter(action="event.snooze_expired", target_id=str(event.pk), actor=None).count() == 1


def test_snooze_suppresses_same_priority_notifications_but_upgrade_releases(metric_scope):
    from insight.metrics.service import scan_object
    from tests_v2.test_metrics import daily_rows, report
    users, shops = metric_scope
    start, today = date(2026,8,1), date(2026,9,5)
    source, _ = report(metric_scope,"sales_traffic",daily_rows(start,last_units=10),start,today-timedelta(days=1))
    scan_object(users["admin"],shops[0],"B000TEST01",today=today)
    events = list(BusinessEvent.objects.all())
    assert events
    old_count = Notification.objects.count()
    for event in events:
        event.snoozed_until = timezone.now()+timedelta(days=1)
        event.save()
    report(metric_scope,"sales_traffic",daily_rows(start,last_units=0),start,today-timedelta(days=1),existing=source)
    scan_object(users["admin"],shops[0],"B000TEST01",today=today)
    assert Notification.objects.count() == old_count
    assert all(BusinessEvent.objects.get(pk=e.pk).snoozed_until for e in events)
    BusinessEvent.objects.all().update(priority=3,notified_priority=3)
    report(metric_scope,"sales_traffic",daily_rows(start,last_units=1),start,today-timedelta(days=1),existing=source)
    scan_object(users["admin"],shops[0],"B000TEST01",today=today)
    assert Notification.objects.count() > old_count
    assert not BusinessEvent.objects.filter(snoozed_until__isnull=False).exists()
