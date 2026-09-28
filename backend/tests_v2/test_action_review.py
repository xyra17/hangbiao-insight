from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from insight.models import ActionTask, AuditEntry, MetricSnapshot

pytestmark = pytest.mark.django_db


def test_completed_action_is_not_reviewed_before_full_days_or_cohort_maturity(metric_scope, client):
    users, shops = metric_scope
    now = timezone.now()
    task = ActionTask.objects.create(shop=shops[0], child_asin="B000TEST01", title="核查退货", created_by=users["a"],
        status="completed", effective_at=now - timedelta(days=3), observed_metrics=["returns"])
    client.force_authenticate(users["a"])
    def review(snapshot=None):
        task.refresh_from_db()
        return client.post(f"/api/v1/action-tasks/{task.pk}/review/", {
            "expected_version":task.version, "status":"completed", "result":"退货订单率下降，尚不能证明因果",
            "review_at": timezone.now().isoformat(),
            **({"snapshot":str(snapshot.pk)} if snapshot else {}),
        }, format="json")
    assert review().status_code == 400
    task.effective_at = now - timedelta(days=40)
    task.save()
    day = task.effective_at.astimezone(ZoneInfo(shops[0].timezone)).date()
    snapshot = MetricSnapshot.objects.create(shop=shops[0], actor=users["a"], child_asin=task.child_asin,
        fingerprint=uuid4().hex, rule_version="test", source_manifest={}, local_date=day + timedelta(days=40),
        payload={"cohort":{"returns":{"status":"missing"}}})
    assert review(snapshot).status_code == 400
    snapshot.payload={"cohort":{"returns":{"status":"ready", "baseline":{
        "start":str(day-timedelta(days=7)), "end":str(day-timedelta(days=1)),
        "rate_status":"ready", "rate":"0.12", "immature_orders":0,
        "unverified_first_shipment_orders":0,
    }, "current":{
        "start":str(day+timedelta(days=1)), "end":str(day+timedelta(days=7)),
        "rate_status":"ready", "rate":"0.08", "immature_orders":0,
        "unverified_first_shipment_orders":0,
    }}}}
    snapshot.save()
    assert review(snapshot).status_code == 200
    recorded = AuditEntry.objects.get(action="action_task.reviewed", target_id=str(task.pk)).after
    assert recorded["snapshot"] == str(snapshot.pk)
    assert recorded["comparison"]["returns"]["current"] == "0.08"
    assert recorded["comparison"]["returns"]["delta"] == "-0.04"
    assert recorded["comparison"]["returns"]["method"] == "test"
    assert recorded["comparison"]["returns"]["maturity"]["status"] == "ready"
    task.refresh_from_db()
    assert task.review_comparison["returns"]["snapshot"] == str(snapshot.pk)


@pytest.mark.parametrize(
    "window_start,window_end,review_offset,expected_status",
    [
        (1, 7, 8, 200),  # exactly seven complete site days, review on day 8
        (1, 6, 8, 400),  # one day short
        (1, 7, 7, 400),  # review happens during the seventh site day
        (1, 7, 8, 400),  # current window marked incomplete below
    ],
)
def test_review_requires_complete_seven_day_window_and_past_review_time(
    metric_scope, client, window_start, window_end, review_offset, expected_status
):
    users, shops = metric_scope
    now = timezone.now()
    task = ActionTask.objects.create(
        shop=shops[0],
        child_asin="B000TEST01",
        title="核查销量",
        created_by=users["a"],
        status="completed",
        effective_at=now - timedelta(days=20),
        observed_metrics=["ordered_units"],
    )
    day = task.effective_at.astimezone(ZoneInfo(shops[0].timezone)).date()
    payload = {
        "daily": {
            "status": "ready",
            "method": "daily_complete_window_v1",
            "baseline": {
                "start": str(day - timedelta(days=28)),
                "end": str(day - timedelta(days=1)),
                "complete": True,
                "ordered_units": 10,
            },
            "current": {
                "start": str(day + timedelta(days=window_start)),
                "end": str(day + timedelta(days=window_end)),
                "ordered_units": 8,
                "complete": False if window_start == 1 and window_end == 7 and expected_status == 400 else True,
            },
        }
    }
    snapshot = MetricSnapshot.objects.create(
        shop=shops[0],
        actor=users["a"],
        child_asin=task.child_asin,
        fingerprint=uuid4().hex,
        rule_version="review-test-v1",
        source_manifest={},
        local_date=day + timedelta(days=8),
        payload=payload,
    )
    client.force_authenticate(users["a"])
    response = client.post(
        f"/api/v1/action-tasks/{task.pk}/review/",
        {
            "expected_version": task.version,
            "status": "completed",
            "result": "完成窗口对比",
            "review_at": (task.effective_at + timedelta(days=review_offset)).isoformat(),
            "snapshot": str(snapshot.pk),
        },
        format="json",
    )
    assert response.status_code == expected_status, response.data
    if expected_status == 200:
        assert response.data["review_comparison"]["ordered_units"]["delta"] == -2


def test_review_rejects_future_timestamp_source_version_and_immature_cohort(metric_scope, client):
    users, shops = metric_scope
    now = timezone.now()
    task = ActionTask.objects.create(
        shop=shops[0], child_asin="B000TEST01", title="核查退货", created_by=users["a"],
        status="completed", effective_at=now - timedelta(days=30), observed_metrics=["returns"],
    )
    day = task.effective_at.astimezone(ZoneInfo(shops[0].timezone)).date()
    snapshot = MetricSnapshot.objects.create(
        shop=shops[0], actor=users["a"], child_asin=task.child_asin, fingerprint=uuid4().hex,
        rule_version="review-test-v1", source_manifest={}, local_date=day + timedelta(days=8),
        payload={"cohort": {"returns": {"status": "ready", "current": {
            "start": str(day + timedelta(days=1)), "end": str(day + timedelta(days=7)),
            "rate_status": "ready", "rate": "0.10", "immature_orders": 1,
            "unverified_first_shipment_orders": 0,
        }, "baseline": {
            "start": str(day - timedelta(days=7)), "end": str(day - timedelta(days=1)),
            "rate_status": "ready", "rate": "0.12", "immature_orders": 0,
            "unverified_first_shipment_orders": 0,
        }}}},
    )
    client.force_authenticate(users["a"])
    base = {
        "expected_version": task.version, "status": "completed", "result": "完成复查",
        "review_at": (task.effective_at + timedelta(days=8)).isoformat(), "snapshot": str(snapshot.pk),
    }
    assert client.post(f"/api/v1/action-tasks/{task.pk}/review/", {**base, "review_at": (now + timedelta(days=1)).isoformat()}, format="json").status_code == 400
    assert client.post(f"/api/v1/action-tasks/{task.pk}/review/", base, format="json").status_code == 400
    snapshot.payload["cohort"]["returns"]["current"]["immature_orders"] = 0
    snapshot.source_manifest = {"new": {"kind": "orders", "revision": 2}}
    snapshot.save(update_fields=["payload", "source_manifest"])
    assert client.post(f"/api/v1/action-tasks/{task.pk}/review/", base, format="json").status_code == 400


def test_review_rejects_missing_values_incomplete_window_and_late_snapshot(metric_scope, client):
    users, shops = metric_scope
    task = ActionTask.objects.create(
        shop=shops[0], child_asin="B000TEST01", title="核查销量", created_by=users["a"],
        status="completed", effective_at=timezone.now() - timedelta(days=20), observed_metrics=["ordered_units"],
    )
    day = task.effective_at.astimezone(ZoneInfo(shops[0].timezone)).date()
    snapshot = MetricSnapshot.objects.create(
        shop=shops[0], actor=users["a"], child_asin=task.child_asin, fingerprint=uuid4().hex,
        rule_version="review-test-v1", source_manifest={}, local_date=day + timedelta(days=8),
        payload={"daily": {"status": "ready", "baseline": {
            "start": str(day - timedelta(days=28)), "end": str(day - timedelta(days=1)),
            "complete": True, "ordered_units": None,
        }, "current": {
            "start": str(day + timedelta(days=1)), "end": str(day + timedelta(days=7)),
            "complete": True, "ordered_units": 8,
        }}},
    )
    client.force_authenticate(users["a"])
    payload = {
        "expected_version": task.version, "status": "completed", "result": "完成复查",
        "review_at": (task.effective_at + timedelta(days=8)).isoformat(), "snapshot": str(snapshot.pk),
    }
    assert client.post(f"/api/v1/action-tasks/{task.pk}/review/", payload, format="json").status_code == 400
    snapshot.payload["daily"]["baseline"]["ordered_units"] = 10
    snapshot.payload["daily"]["current"].pop("complete")
    snapshot.save(update_fields=["payload"])
    assert client.post(f"/api/v1/action-tasks/{task.pk}/review/", payload, format="json").status_code == 400
    snapshot.payload["daily"]["current"]["complete"] = True
    snapshot.local_date = day + timedelta(days=9)
    snapshot.save(update_fields=["payload", "local_date"])
    assert client.post(f"/api/v1/action-tasks/{task.pk}/review/", payload, format="json").status_code == 400


def test_formal_action_requires_complete_assignment_and_execution_fields(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["a"])
    data={"shop":str(shops[0].pk), "child_asin":"B000TEST01", "title":"验证包装改善", "is_formal":True}
    assert client.post("/api/v1/action-tasks/", data, format="json").status_code == 400
    data.update(assigned_to=users["a"].pk, due_at=(timezone.now()+timedelta(days=1)).isoformat(),
        question="破损是否减少", completion_criteria="记录包装核验与前后对比", observed_metrics=["returns"])
    response=client.post("/api/v1/action-tasks/", data, format="json")
    assert response.status_code == 201
    task=ActionTask.objects.get(pk=response.data["id"])
    result=client.post(f"/api/v1/action-tasks/{task.pk}/complete/", {"expected_version":1, "notes":"已分派"}, format="json")
    assert result.status_code == 400 and not task.executions.exists()
