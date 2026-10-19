from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from django.db.models import Q
from django.utils import timezone

from insight.metrics.facts import source_manifest
from insight.models import ActionTask, AuditEntry, MetricSnapshot, ProductMapping
from tests_v2.test_metrics import report

pytestmark = pytest.mark.django_db


def create_review_snapshot(task, actor, manifest, local_date, *, configuration=None):
    mappings = ProductMapping.objects.filter(
        product__shop=task.shop,
        product__child_asin=task.child_asin,
        effective_from__lte=local_date,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=local_date))
    if task.product_id:
        mappings = mappings.filter(product_id=task.product_id)
    return MetricSnapshot.objects.create(
        shop=task.shop,
        actor=actor,
        child_asin=task.child_asin,
        product_id=task.product_id,
        fingerprint=uuid4().hex,
        rule_version="review-test-v1",
        source_manifest=manifest,
        local_date=local_date,
        payload={
            "mode": "live",
            "mapping_versions": {str(mapping.pk): mapping.version for mapping in mappings},
            "configuration": {"values": configuration or {}},
        },
    )


def daily_source_rows(start, end, *, missing_day=None, missing_units_day=None):
    rows = []
    day = start
    while day <= end:
        if day != missing_day:
            rows.append(
                {
                    "business_date": day,
                    "ordered_units": (
                        None
                        if day == missing_units_day
                        else 999
                        if day == start + timedelta(days=28)
                        else 10
                        if day < start + timedelta(days=28)
                        else 8
                    ),
                    "order_count": 2,
                    "sessions": 100,
                }
            )
        day += timedelta(days=1)
    return rows


def test_completed_action_is_not_reviewed_before_full_days_or_cohort_maturity(metric_scope, client):
    users, shops = metric_scope
    now = timezone.now()
    task = ActionTask.objects.create(
        shop=shops[0],
        child_asin="B000TEST01",
        title="核查退货",
        created_by=users["a"],
        status="completed",
        effective_at=now - timedelta(days=3),
        observed_metrics=["returns"],
    )
    client.force_authenticate(users["a"])

    def review(snapshot=None):
        task.refresh_from_db()
        return client.post(
            f"/api/v1/action-tasks/{task.pk}/review/",
            {
                "expected_version": task.version,
                "status": "completed",
                "result": "退货订单率下降，尚不能证明因果",
                "review_at": timezone.now().isoformat(),
                **({"snapshot": str(snapshot.pk)} if snapshot else {}),
            },
            format="json",
        )

    assert review().status_code == 400
    task.effective_at = now - timedelta(days=100)
    task.save()
    day = task.effective_at.astimezone(ZoneInfo(shops[0].timezone)).date()
    snapshot = create_review_snapshot(task, users["a"], {}, day + timedelta(days=40))
    assert review(snapshot).status_code == 400

    baseline_start = day - timedelta(days=58)
    baseline_end = day - timedelta(days=31)
    current_start = day + timedelta(days=1)
    current_end = day + timedelta(days=28)
    order_rows = []
    order_by_day = {}
    cursor = baseline_start
    while cursor <= current_end:
        order_id = f"ORDER-{cursor:%Y%m%d}"
        line_id = f"LINE-{cursor:%Y%m%d}"
        row = {
            "business_date": cursor,
            "seller_sku": "S1",
            "fulfillment": "FBA",
            "record_id": line_id,
            "order_id": order_id,
            "quantity": 1,
            "shipped_quantity": 1,
            "shipped_date": cursor,
            "status": "shipped",
        }
        order_rows.append(row)
        order_by_day[cursor] = row
        cursor += timedelta(days=1)
    report(
        metric_scope,
        "orders",
        order_rows,
        baseline_start,
        current_end,
        "product_record",
        "shipped_date",
    )

    return_rows = []
    for index, shipped_day in enumerate((baseline_start + timedelta(days=1), current_start + timedelta(days=1), current_start + timedelta(days=2)), 1):
        received_day = shipped_day + timedelta(days=20)
        return_rows.append(
            {
                "business_date": received_day,
                "seller_sku": "S1",
                "fulfillment": "FBA",
                "record_id": f"RETURN-{index}",
                "order_line_id": order_by_day[shipped_day]["record_id"],
                "quantity": 1,
                "status": "received",
                "requested_date": received_day - timedelta(days=1),
                "received_date": received_day,
            }
        )
    report(
        metric_scope,
        "returns",
        return_rows,
        baseline_start,
        now.astimezone(ZoneInfo(shops[0].timezone)).date() - timedelta(days=1),
        "product_record",
    )
    review_day = now.astimezone(ZoneInfo(shops[0].timezone)).date()
    snapshot = create_review_snapshot(task, users["a"], source_manifest(shops[0]), review_day)
    assert review(snapshot).status_code == 200
    recorded = AuditEntry.objects.get(action="action_task.reviewed", target_id=str(task.pk)).after
    assert recorded["snapshot"] == str(snapshot.pk)
    assert recorded["comparison"]["returns"]["current"] == str(Decimal(2) / Decimal(28))
    assert recorded["comparison"]["returns"]["baseline"] == str(Decimal(1) / Decimal(28))
    assert recorded["comparison"]["returns"]["basis"] == "mature_cohort_rate"
    assert recorded["comparison"]["returns"]["method"] == "action_effect_windows_v1"
    assert recorded["comparison"]["returns"]["maturity"]["status"] == "ready"
    assert recorded["comparison"]["returns"]["window_evidence"]["baseline"]["denominator"] == 28
    assert recorded["comparison"]["returns"]["window_evidence"]["current"]["denominator"] == 28
    assert recorded["comparison"]["returns"]["window_evidence"]["baseline"]["start"] == str(baseline_start)
    assert recorded["comparison"]["returns"]["window_evidence"]["baseline"]["end"] == str(baseline_end)
    assert recorded["comparison"]["returns"]["window_evidence"]["current"]["start"] == str(current_start)
    assert recorded["comparison"]["returns"]["window_evidence"]["current"]["end"] == str(current_end)
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
    baseline_start = day - timedelta(days=28)
    reported_end = day + timedelta(days=window_end)
    missing_day = day + timedelta(days=4) if expected_status == 400 and window_start == 1 and window_end == 7 and review_offset == 8 else None
    rows = daily_source_rows(baseline_start, reported_end, missing_day=missing_day)
    report(metric_scope, "sales_traffic", rows, baseline_start, reported_end)
    snapshot = create_review_snapshot(
        task,
        users["a"],
        source_manifest(shops[0]),
        day + timedelta(days=max(window_end + 1, 7)),
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
        comparison = response.data["review_comparison"]["ordered_units"]
        assert comparison["baseline"] == "10"
        assert comparison["current"] == "8"
        assert comparison["delta"] == "-2"
        assert comparison["unit"] == "units/day"
        assert comparison["basis"] == "daily_mean"
        assert comparison["baseline_days"] == 28
        assert comparison["current_days"] == 7
        assert comparison["baseline_total"] == "280"
        assert comparison["current_total"] == "56"
        assert comparison["window_evidence"]["effective_date"] == str(day)
        assert comparison["window_evidence"]["baseline"]["start"] == str(day - timedelta(days=28))
        assert comparison["window_evidence"]["baseline"]["end"] == str(day - timedelta(days=1))
        assert comparison["window_evidence"]["current"]["start"] == str(day + timedelta(days=1))
        assert comparison["window_evidence"]["current"]["end"] == str(day + timedelta(days=7))
        assert len(comparison["window_evidence"]["baseline"]["raw_row_ids"]) == 28
        assert len(comparison["window_evidence"]["current"]["raw_row_ids"]) == 7


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
    baseline_start = day - timedelta(days=28)
    current_end = day + timedelta(days=7)
    rows = daily_source_rows(baseline_start, current_end, missing_units_day=baseline_start)
    source, _ = report(metric_scope, "sales_traffic", rows, baseline_start, current_end)
    snapshot = create_review_snapshot(task, users["a"], source_manifest(shops[0]), day + timedelta(days=8))
    client.force_authenticate(users["a"])
    payload = {
        "expected_version": task.version, "status": "completed", "result": "完成复查",
        "review_at": (task.effective_at + timedelta(days=8)).isoformat(), "snapshot": str(snapshot.pk),
    }
    assert client.post(f"/api/v1/action-tasks/{task.pk}/review/", payload, format="json").status_code == 400

    rows = daily_source_rows(baseline_start, current_end, missing_day=day + timedelta(days=4))
    report(metric_scope, "sales_traffic", rows, baseline_start, current_end, existing=source)
    snapshot = create_review_snapshot(task, users["a"], source_manifest(shops[0]), day + timedelta(days=8))
    payload["snapshot"] = str(snapshot.pk)
    assert client.post(f"/api/v1/action-tasks/{task.pk}/review/", payload, format="json").status_code == 400

    rows = daily_source_rows(baseline_start, current_end)
    report(metric_scope, "sales_traffic", rows, baseline_start, current_end, existing=source)
    snapshot = create_review_snapshot(task, users["a"], source_manifest(shops[0]), day + timedelta(days=9))
    payload["snapshot"] = str(snapshot.pk)
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


def test_source_bound_action_requires_explicit_confirmation_and_both_frozen_inputs(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["a"])
    payload = {
        "shop": str(shops[0].pk),
        "child_asin": "B000TEST01",
        "title": "基于冻结调查的复查任务",
        "source_report": str(uuid4()),
        "source_investigation": str(uuid4()),
    }
    response = client.post("/api/v1/action-tasks/", payload, format="json")
    assert response.status_code == 400
    assert "明确确认" in str(response.data)

    payload.pop("source_investigation")
    payload.update(confirm_source=True, confirmation_note="已核对来源")
    response = client.post("/api/v1/action-tasks/", payload, format="json")
    assert response.status_code == 400
    assert "同时指定报告和调查" in str(response.data)
