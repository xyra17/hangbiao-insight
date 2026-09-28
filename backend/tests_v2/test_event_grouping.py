from datetime import date
from uuid import uuid4

import pytest
from django.utils import timezone

from insight.models import AuditEntry, BusinessEvent, EventGroupingReceipt

pytestmark = pytest.mark.django_db


def events(scope):
    _, shops = scope
    return [BusinessEvent.objects.create(shop=shops[0], child_asin="B000TEST01", object_key="asin:B000TEST01", family=family, title=family, priority=i, last_cutoff=date(2026,9,20), last_seen=timezone.now()) for i, family in enumerate(["sales", "returns", "safety"])]


def command(client, rows, operation="merge", **extra):
    body = {"operation": operation, "events": {str(e.pk): e.version for e in rows}, "title":"包装破损排查", "reason":"人工核实相关证据", "idempotency_key":uuid4().hex, **extra}
    return client.post(f"/api/v1/events/{rows[0].pk}/group-command/", body, format="json"), body


def test_cross_family_group_split_preserves_risk_and_idempotency(metric_scope, client):
    rows = events(metric_scope)
    client.force_authenticate(metric_scope[0]["a"])
    response, body = command(client, rows)
    assert response.status_code == 200, response.data
    group = response.data["group_id"]
    response, _ = command(client, rows, **body)
    assert response.status_code == 200 and response.data["group_id"] == group
    assert EventGroupingReceipt.objects.count() == 1
    for i, item in enumerate(rows):
        item.refresh_from_db()
        assert str(item.group_id) == group and item.priority == i and item.status == "pending"
    response, _ = command(client, [rows[0]], operation="split")
    assert response.status_code == 200 and response.data["group_id"] is None
    assert AuditEntry.objects.filter(action__in=["event.merge", "event.split"]).count() == 2
    related = client.get(f"/api/v1/events/{rows[0].pk}/related/")
    assert related.status_code == 200 and related.data["count"] == 2


def test_group_scope_stale_version_and_explicit_members(metric_scope, client):
    rows = events(metric_scope)
    client.force_authenticate(metric_scope[0]["b"])
    assert command(client, rows)[0].status_code == 404
    client.force_authenticate(metric_scope[0]["a"])
    assert command(client, rows[:2])[0].status_code == 200
    assert command(client, rows[:2])[0].status_code == 409
    for e in rows:
        e.refresh_from_db()
    assert command(client, [rows[0],rows[2]])[0].status_code == 409
    rows[2].child_asin = "B000OTHER1"
    rows[2].save()
    assert command(client, rows)[0].status_code == 400


def test_group_validation_collision_and_audit_rollback(metric_scope, client, monkeypatch):
    from insight.metrics import grouping
    rows = events(metric_scope)
    client.force_authenticate(metric_scope[0]["a"])
    assert command(client, rows, events={})[0].status_code == 400
    def broken(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(grouping, "audit", broken)
    assert command(client, rows)[0].status_code == 500
    assert not EventGroupingReceipt.objects.exists()
    assert not BusinessEvent.objects.filter(group__isnull=False).exists()


def test_event_queue_unknown_impact_last_within_priority(metric_scope, client):
    rows = events(metric_scope)
    BusinessEvent.objects.filter(pk__in=[r.pk for r in rows]).update(priority=1)
    BusinessEvent.objects.filter(pk=rows[0].pk).update(impact=None, magnitude=99)
    BusinessEvent.objects.filter(pk=rows[1].pk).update(impact=2, magnitude=1)
    BusinessEvent.objects.filter(pk=rows[2].pk).update(priority=0, impact=None, magnitude=0)
    client.force_authenticate(metric_scope[0]["a"])
    data = client.get("/api/v1/events/").data["results"]
    assert [r["id"] for r in data] == [str(rows[2].pk), str(rows[1].pk), str(rows[0].pk)]


def test_group_key_cannot_be_reused_for_different_intent(metric_scope, client):
    rows = events(metric_scope)
    client.force_authenticate(metric_scope[0]["a"])
    result, body = command(client, rows)
    assert result.status_code == 200
    body["reason"] = "改成其他理由"
    assert command(client, rows, **body)[0].status_code == 409
