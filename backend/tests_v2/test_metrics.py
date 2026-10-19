import csv
import io
from datetime import date, datetime, timedelta, timezone

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from insight.ingestion.service import accept_upload, create_source, parse_batch, publish_batch
from insight.metrics.cohort import calculate_cohort
from insight.metrics.daily import calculate_daily
from insight.metrics.facts import source_manifest
from insight.metrics.service import scan_object
from insight.metrics.statistics import newcombe, wilson
from insight.models import BusinessEvent, EventEvidence, Notification, Product, ProductMapping

pytestmark = pytest.mark.django_db


def report(scope, kind, rows, start, end, grain="asin_day", basis="business_date", existing=None):
    users, shops = scope
    rows = [{"child_asin": "B000TEST01", **row} for row in rows]
    fields = list(rows[0])
    source = existing or create_source(
        users["admin"],
        shops[0],
        {
            "name": kind,
            "kind": kind,
            "is_primary": True,
            "grain": grain,
            "coverage_basis": basis,
            "column_mapping": {field: field for field in fields},
            "ignored_columns": [],
        },
        "fixture",
    )
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    batch = accept_upload(
        users["admin"],
        source,
        SimpleUploadedFile(f"{kind}.csv", handle.getvalue().encode()),
        {
            "coverage_start": start,
            "coverage_end": end,
            "generated_at": datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc),
            "declared_complete": True,
            "declared_asins": sorted({row["child_asin"] for row in rows}),
        },
        "fixture",
    )
    parse_batch(batch.pk, users["admin"])
    batch.refresh_from_db()
    assert batch.error_rows == 0, list(batch.rows.exclude(error="").values("error"))
    publish_batch(users["admin"], batch.pk, allow_partial=False, confirm_contract=True, request_id="fixture")
    return source, batch


def daily_rows(start, last_units=4):
    return [
        {
            "business_date": start + timedelta(days=i),
            "ordered_units": 20 if i < 28 else last_units,
            "order_count": 10 if i < 28 else 2,
            "sessions": 100,
        }
        for i in range(35)
    ]


def test_daily_aggregates_native_grain_and_scan_is_idempotent(metric_scope, client):
    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    snapshot = scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    assert snapshot.payload["daily"]["current"]["ordered_units"] == 28
    assert snapshot.payload["daily"]["current"]["unit_session_percent"] == "4.00"
    assert snapshot.payload["daily"]["current"]["order_count"] == 14
    assert len(snapshot.payload["daily"]["signals"]) == 2
    assert BusinessEvent.objects.count() == 1
    assert EventEvidence.objects.count() == 2
    notifications = Notification.objects.count()
    assert scan_object(users["admin"], shops[0], "B000TEST01", today=today).pk == snapshot.pk
    scan_object(users["admin"], shops[0], "B000TEST01", today=today + timedelta(days=1))
    assert EventEvidence.objects.count() == 2  # A calendar tick is not new data.
    assert Notification.objects.count() == notifications
    assert BusinessEvent.objects.get().consecutive_days == 1
    client.force_authenticate(users["b"])
    assert client.get(f"/api/v1/snapshots/{snapshot.pk}/").status_code == 404
    assert client.get("/api/v1/events/").json()["count"] == 0


def test_snapshot_list_caches_raw_source_permission_per_shop_for_one_request(metric_scope):
    from unittest.mock import patch
    from uuid import uuid4

    from django.test import RequestFactory

    from insight.metrics.api import SnapshotSerializer
    from insight.models import MetricSnapshot

    users, shops = metric_scope
    snapshots = []
    for index, shop in enumerate([shops[0], shops[0], shops[1]], start=1):
        product = Product.objects.filter(shop=shop).first()
        snapshots.append(
            MetricSnapshot.objects.create(
                shop=shop,
                child_asin=product.child_asin,
                product=product,
                actor=users["admin"],
                fingerprint=uuid4().hex,
                rule_version="permission-cache-test",
                source_manifest={},
                payload={"cohort": {"refunds": {"baseline": {"order_ids": [f"order-{index}"]}}}},
                local_date=date(2026, 9, 1),
            )
        )

    request = RequestFactory().get("/api/v1/snapshots/")
    request.user = users["a"]
    with patch("insight.metrics.api.can_read_raw_source", return_value=False) as check_access:
        rows = SnapshotSerializer(snapshots, many=True, context={"request": request}).data

    assert check_access.call_count == 2
    assert rows[0]["payload"]["cohort"]["refunds"]["baseline"] == {}
    assert rows[1]["payload"]["cohort"]["refunds"]["baseline"] == {}
    assert rows[2]["payload"]["cohort"]["refunds"]["baseline"] == {}
    assert snapshots[0].payload["cohort"]["refunds"]["baseline"]["order_ids"] == ["order-1"]


def test_missing_day_and_stale_data_never_create_false_sales_signal(metric_scope):
    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    report(metric_scope, "sales_traffic", daily_rows(start)[:-1], start, today - timedelta(days=1))
    result = calculate_daily(source_manifest(shops[0]), "B000TEST01", today)
    assert result["status"] != "ready" and result["signals"] == []
    scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    assert not BusinessEvent.objects.exists()


def test_revision_replaces_business_key_without_erasing_old_snapshot(metric_scope):
    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    source, _ = report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    old = scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    report(
        metric_scope,
        "sales_traffic",
        daily_rows(start, last_units=20),
        start,
        today - timedelta(days=1),
        existing=source,
    )
    new = scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    assert old.pk != new.pk
    assert new.payload["daily"]["current"]["ordered_units"] == 140
    old.refresh_from_db()
    assert old.payload["daily"]["current"]["ordered_units"] == 28


def test_unit_session_percentage_can_exceed_one_hundred_and_zero_is_not_missing(metric_scope):
    _, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    rows = daily_rows(start, last_units=200)
    report(metric_scope, "sales_traffic", rows, start, today - timedelta(days=1))
    result = calculate_daily(source_manifest(shops[0]), "B000TEST01", today)
    assert result["current"]["unit_session_percent"] == "200"
    assert "wilson_95" not in result["current"]


def test_wilson_and_newcombe_known_answers():
    assert wilson(0, 0) is None
    assert wilson(50, 100) == pytest.approx([0.4038315304, 0.5961684696])
    assert newcombe(20, 100, 5, 100)[0] > 0
    assert newcombe(5, 100, 5, 100)[0] < 0 < newcombe(5, 100, 5, 100)[1]


def test_refund_cohort_reversal_exchange_and_observation_boundary(metric_scope):
    _, shops = metric_scope
    orders = [
        {
            "business_date": "2026-06-01",
            "seller_sku": "S1",
            "fulfillment": "FBA",
            "record_id": f"L{i}",
            "order_id": f"O{i}",
            "quantity": 1,
            "shipped_quantity": 1,
            "shipped_date": "2026-06-01",
            "status": "shipped",
            "is_exchange": "true" if i == 4 else "false",
        }
        for i in range(1, 5)
    ]
    report(
        metric_scope, "orders", orders, date(2026, 4, 1), date(2026, 7, 31), "product_record", "shipped_date"
    )
    transactions = [
        ("R1", "L1", "C1", "2026-06-10", "5"),
        ("R2", "L2", "C2", "2026-06-10", "5"),
        ("R3", "L2", "C2", "2026-06-11", "-5"),
        ("R4", "L3", "C3", "2026-07-01", "5"),  # exactly 30 days: outside [start, end)
    ]
    refunds = [
        {
            "business_date": day,
            "seller_sku": "S1",
            "fulfillment": "FBA",
            "record_id": rid,
            "order_line_id": line,
            "transaction_chain_id": chain,
            "amount": amount,
            "currency": "USD",
        }
        for rid, line, chain, day, amount in transactions
    ]
    report(metric_scope, "refunds", refunds, date(2026, 4, 1), date(2026, 7, 31), "product_record")
    result = calculate_cohort(source_manifest(shops[0]), "B000TEST01", date(2026, 8, 1), shops[0].timezone)
    assert result["refunds"]["status"] == "ready"
    assert result["refunds"]["baseline"]["denominator"] == 3
    assert result["refunds"]["baseline"]["numerator"] == 1
    assert result["refunds"]["late_order_count"] == 1
    assert result["signals"] == []  # low sample, never a quality alarm


def test_import_and_daily_schedule_use_idempotent_jobs_and_recheck_permissions(metric_scope):
    from unittest.mock import patch

    from insight.metrics.scheduling import dispatch_daily
    from insight.models import DailyScanSchedule, Job, Membership

    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    assert Job.objects.filter(kind="scan_metrics").count() == 2
    schedule = DailyScanSchedule.objects.get(shop=shops[0])
    with patch(
        "insight.metrics.scheduling.timezone.now", return_value=datetime(2026, 9, 5, 10, tzinfo=timezone.utc)
    ):
        dispatch_daily(schedule.pk)
        count = Job.objects.filter(kind="scan_metrics").count()
        dispatch_daily(schedule.pk)
        assert Job.objects.filter(kind="scan_metrics").count() == count == 4
    Membership.objects.filter(user=users["admin"]).update(is_admin=False)
    with patch(
        "insight.metrics.scheduling.timezone.now", return_value=datetime(2026, 9, 6, 10, tzinfo=timezone.utc)
    ):
        dispatch_daily(schedule.pk)
    schedule.refresh_from_db()
    assert "权限已撤销" in schedule.error
    assert Job.objects.filter(kind="scan_metrics").count() == 4


def test_trace_hides_ignored_columns_and_asin_snapshot_requires_all_skus(metric_scope, client):
    from insight.models import ShopGrant

    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    _, batch = report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    row = batch.rows.first()
    row.raw["buyer_phone"] = "private-source-column"
    row.save(update_fields=["raw"])
    snapshot = scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    client.force_authenticate(users["a"])
    response = client.get(f"/api/v1/fact-rows/{row.pk}/")
    assert response.status_code == 200
    assert "buyer_phone" not in response.json()["raw"]
    client.force_authenticate(users["b"])
    assert client.get(f"/api/v1/fact-rows/{row.pk}/").status_code == 404
    original = Product.objects.get(shop=shops[0])
    Product.objects.create(shop=shops[0], child_asin="B000TEST01", seller_sku="S2", fulfillment="FBA")
    grant = ShopGrant.objects.get(user=users["restricted"])
    grant.products.add(original)
    client.force_authenticate(users["restricted"])
    assert client.get(f"/api/v1/snapshots/{snapshot.pk}/").status_code == 404
    assert client.get(f"/api/v1/fact-rows/{row.pk}/").status_code == 404


def test_zero_sessions_is_not_applicable_and_total_stock_does_not_hide_stockout(metric_scope):
    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    rows = daily_rows(start)
    for row in rows[28:]:
        row["sessions"] = 0
    report(metric_scope, "sales_traffic", rows, start, today - timedelta(days=1))
    report(
        metric_scope,
        "inventory",
        [
            {"business_date": today - timedelta(days=2), "available": 5, "total": 20},
            {"business_date": today - timedelta(days=1), "available": 0, "total": 12},
        ],
        today - timedelta(days=2),
        today - timedelta(days=1),
    )
    snapshot = scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    assert snapshot.payload["daily"]["current"]["ratio_status"] == "not_applicable"
    assert snapshot.payload["daily"]["current"]["unit_session_percent"] is None
    event = BusinessEvent.objects.get(family="availability")
    assert event.priority == 0
    assert snapshot.payload["absolute"]["inventory"]["total"] == 12


def test_schedule_configuration_detects_conflicts_and_rejects_operator(metric_scope, client):
    users, shops = metric_scope
    data = {"shop": str(shops[0].pk), "at": "08:00:00", "enabled": True, "expected_version": 0}
    client.force_authenticate(users["a"])
    assert client.post("/api/v1/scan-schedules/", data, format="json").status_code == 403
    client.force_authenticate(users["admin"])
    assert client.post("/api/v1/scan-schedules/", data, format="json").status_code == 200
    assert client.post("/api/v1/scan-schedules/", data, format="json").status_code == 409


def test_record_correction_moves_object_without_resurrecting_old_version(metric_scope):
    from insight.metrics.facts import latest_facts
    from insight.models import Job

    _, shops = metric_scope
    other = Product.objects.create(shop=shops[0], child_asin="B000TEST02", seller_sku="S2", fulfillment="FBA")
    ProductMapping.objects.create(
        product=other, name="第二商品", effective_from=date(2025, 1, 1), version=1, reason="fixture"
    )
    original = {
        "business_date": "2026-06-01",
        "seller_sku": "S1",
        "fulfillment": "FBA",
        "record_id": "L1",
        "order_id": "O1",
        "quantity": 1,
        "shipped_quantity": 1,
        "shipped_date": "2026-06-01",
        "status": "shipped",
    }
    source, _ = report(
        metric_scope,
        "orders",
        [original],
        date(2026, 6, 1),
        date(2026, 6, 1),
        "product_record",
        "shipped_date",
    )
    old = source_manifest(shops[0])
    _, batch = report(
        metric_scope,
        "orders",
        [{**original, "child_asin": "B000TEST02", "seller_sku": "S2"}],
        date(2026, 6, 1),
        date(2026, 6, 1),
        "product_record",
        "shipped_date",
        existing=source,
    )
    current = source_manifest(shops[0])
    assert latest_facts(old, "orders", "B000TEST01").count() == 1
    assert latest_facts(current, "orders", "B000TEST01").count() == 0
    assert latest_facts(current, "orders", "B000TEST02").count() == 1
    # Both the old and new object are re-scanned after the correction.
    assert Job.objects.filter(kind="scan_metrics", payload__child_asin="B000TEST01").count() == 4
    assert Job.objects.filter(kind="scan_metrics", payload__child_asin="B000TEST02").count() == 2


def test_persistence_requires_consecutive_matching_scans_with_new_complete_days(metric_scope):
    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    source, _ = report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    assert BusinessEvent.objects.get().consecutive_days == 1
    # A delayed scan with two new complete days is still the next consecutive scan.
    shifted = start + timedelta(days=2)
    report(
        metric_scope,
        "sales_traffic",
        daily_rows(shifted),
        shifted,
        today + timedelta(days=1),
        existing=source,
    )
    second = scan_object(users["admin"], shops[0], "B000TEST01", today=today + timedelta(days=2))
    assert second.payload["daily"]["signals"][0]["consecutive_scans"] == 2
    assert BusinessEvent.objects.get().consecutive_days == 2
    # A healthy scan breaks continuity even though the event remains open until an operator closes it.
    report(
        metric_scope,
        "sales_traffic",
        daily_rows(shifted, last_units=20),
        shifted,
        today + timedelta(days=1),
        existing=source,
    )
    scan_object(users["admin"], shops[0], "B000TEST01", today=today + timedelta(days=2))
    report(
        metric_scope,
        "sales_traffic",
        daily_rows(shifted, last_units=3),
        shifted,
        today + timedelta(days=1),
        existing=source,
    )
    scan_object(users["admin"], shops[0], "B000TEST01", today=today + timedelta(days=2))
    assert BusinessEvent.objects.get().consecutive_days == 1


def test_missing_measurements_preserve_open_event_and_recover_without_duplicate_alert(metric_scope):
    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    source, _ = report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    event = BusinessEvent.objects.get()
    assert not event.needs_data
    notices = Notification.objects.count()
    scan_object(users["admin"], shops[0], "B000TEST01", today=today + timedelta(days=3))
    event.refresh_from_db()
    assert event.needs_data and event.status == "pending"
    assert event.last_checked_at > event.last_seen
    shifted = start + timedelta(days=3)
    report(
        metric_scope,
        "sales_traffic",
        daily_rows(shifted),
        shifted,
        today + timedelta(days=2),
        existing=source,
    )
    scan_object(users["admin"], shops[0], "B000TEST01", today=today + timedelta(days=3))
    event.refresh_from_db()
    assert not event.needs_data
    assert BusinessEvent.objects.count() == 1
    assert Notification.objects.count() == notices


def test_operations_overlap_is_disclosed_without_claiming_causation(metric_scope):
    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    report(
        metric_scope,
        "operations",
        [
            {
                "business_date": date(2026, 8, 28),
                "record_id": "PROMO1",
                "event_type": "promotion",
                "description": "限时促销",
                "ends_on": date(2026, 9, 1),
            }
        ],
        date(2026, 8, 28),
        date(2026, 9, 1),
        "asin_record",
    )
    snapshot = scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    context = snapshot.payload["comparability"]
    assert context["status"] == "limited"
    assert context["operations"][0]["windows"] == ["销售与流量"]
    assert context["operations"][0]["raw_row_id"]
    assert snapshot.payload["daily"]["signals"]


def test_closed_event_needs_new_evidence_to_reopen_and_links_late_recurrence(metric_scope):
    from django.utils import timezone as django_timezone

    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    source, _ = report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    event = BusinessEvent.objects.get()
    event.status, event.closed_at = "closed", django_timezone.now()
    event.save()
    scan_object(users["admin"], shops[0], "B000TEST01", today=today + timedelta(days=1))
    event.refresh_from_db()
    assert event.status == "closed"
    assert not Notification.objects.filter(kind="reopened").exists()
    # Correct the old window: archive revision evidence, never notify a recurrence.
    report(
        metric_scope,
        "sales_traffic",
        daily_rows(start, last_units=2),
        start,
        today - timedelta(days=1),
        existing=source,
    )
    scan_object(users["admin"], shops[0], "B000TEST01", today=today + timedelta(days=1))
    event.refresh_from_db()
    assert event.status == "closed"
    assert event.evidence.filter(detail__evidence_type="historical_revision").exists()
    assert not Notification.objects.filter(kind="reopened").exists()
    shifted = start + timedelta(days=1)
    report(metric_scope, "sales_traffic", daily_rows(shifted), shifted, today, existing=source)
    scan_object(users["admin"], shops[0], "B000TEST01", today=today + timedelta(days=1))
    event.refresh_from_db()
    assert event.status == "pending"
    assert BusinessEvent.objects.count() == 1
    assert Notification.objects.filter(kind="reopened").count() == 2
    assert not Notification.objects.filter(user=users["b"]).exists()
    event.status, event.closed_at = "closed", django_timezone.now() - timedelta(days=31)
    event.save()
    shifted += timedelta(days=1)
    report(
        metric_scope,
        "sales_traffic",
        daily_rows(shifted),
        shifted,
        today + timedelta(days=1),
        existing=source,
    )
    scan_object(users["admin"], shops[0], "B000TEST01", today=today + timedelta(days=2))
    successor = BusinessEvent.objects.exclude(pk=event.pk).get()
    assert successor.reopened_from_id == event.pk


def test_native_asin_parent_change_uses_import_mapping_versions(metric_scope):
    users, shops = metric_scope
    start, today = date(2026, 8, 1), date(2026, 9, 5)
    product = Product.objects.get(shop=shops[0])
    original = product.mappings.get()
    original.parent_asin = "B000PARENT"
    original.save()
    source, _ = report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    before = scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    ProductMapping.objects.create(
        product=product,
        parent_asin="B000NEWPAR",
        name="变体调整",
        effective_from=start + timedelta(days=28),
        version=2,
        reason="验收父体变化",
    )
    product.version = 2
    product.save()
    report(
        metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1), existing=source
    )
    after = scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    assert after.payload["comparability"]["status"] == "limited"
    assert after.payload["comparability"]["structure_changes"][0]["parent_asins"] == [
        "B000NEWPAR",
        "B000PARENT",
    ]
    before.refresh_from_db()
    assert not before.payload["comparability"]["structure_changes"]


def test_historical_recompute_saves_trace_without_mutating_live_events(metric_scope, client):
    from zoneinfo import ZoneInfo

    from django.utils import timezone as django_timezone

    from insight.models import Job, MetricSnapshot
    from insight.tasks import run_job

    users, shops = metric_scope
    today = django_timezone.now().astimezone(ZoneInfo(shops[0].timezone)).date()
    start = today - timedelta(days=36)
    report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=2))
    scan_object(users["admin"], shops[0], "B000TEST01", today=today)
    event = BusinessEvent.objects.get()
    evidence_count, notices = event.evidence.count(), Notification.objects.count()
    body = {
        "shop": str(shops[0].pk),
        "child_asin": "B000TEST01",
        "as_of": str(today - timedelta(days=1)),
        "idempotency_key": "history-test",
    }
    client.force_authenticate(users["a"])
    assert client.post("/api/v1/snapshots/recompute/", body, format="json").status_code == 403
    client.force_authenticate(users["admin"])
    response = client.post("/api/v1/snapshots/recompute/", body, format="json")
    assert response.status_code == 202
    run_job(response.data["id"])
    job = Job.objects.get(pk=response.data["id"])
    assert job.status == "completed", job.error
    historical = MetricSnapshot.objects.get(pk=job.result["snapshot_id"])
    assert historical.payload["mode"] == "historical"
    assert event.evidence.count() == evidence_count
    assert Notification.objects.count() == notices
    assert (
        client.post("/api/v1/snapshots/recompute/", body | {"as_of": str(today)}, format="json").status_code
        == 400
    )


def test_historical_range_recompute_is_idempotent_and_has_no_event_side_effect(metric_scope, client):
    from insight.models import Job, MetricSnapshot
    from insight.tasks import run_job

    users, shops = metric_scope
    start = date(2026, 8, 1)
    report(metric_scope, "sales_traffic", daily_rows(start), start, date(2026, 9, 4))
    body = {
        "shop": str(shops[0].pk),
        "child_asin": "B000TEST01",
        "from_date": "2026-08-20",
        "to_date": "2026-08-22",
        "idempotency_key": "range-history-test",
    }
    client.force_authenticate(users["admin"])
    response = client.post("/api/v1/snapshots/recompute-range/", body, format="json")
    assert response.status_code == 202
    assert (
        client.post("/api/v1/snapshots/recompute-range/", body, format="json").data["id"]
        == response.data["id"]
    )
    run_job(response.data["id"])
    job = Job.objects.get(pk=response.data["id"])
    assert job.status == "completed", job.error
    assert len(job.result["snapshot_ids"]) == 3
    assert MetricSnapshot.objects.filter(payload__mode="historical").count() == 3
    assert not BusinessEvent.objects.exists()
    assert not Notification.objects.exists()


def test_future_shipments_and_ignored_returns_do_not_pollute_cohort():
    from types import SimpleNamespace as Row

    from insight.metrics.cohort import cohort_from_facts

    days = {date(2026, 4, 1) + timedelta(days=i): "ready" for i in range(122)}
    orders = [
        Row(
            record_id=f"L{i}",
            raw_row_id=f"raw-L{i}",
            order_id=f"O{i}",
            business_date=date(2026, 6, 1),
            shipped_quantity=1,
            shipped_at=None,
            shipped_date=day,
        )
        for i, day in enumerate([date(2026, 6, 10), date(2026, 8, 10)])
    ]
    returns = [
        Row(
            order_line_id="unknown",
            received_at=None,
            received_date=day,
            status=status,
            batch=Row(contract=Row(return_event="received")),
        )
        for day, status in [(date(2026, 8, 5), "received"), (date(2026, 6, 20), "cancelled")]
    ]
    result = cohort_from_facts(
        days, {"refunds": days, "returns": days}, orders, [], returns, date(2026, 8, 1), "America/Los_Angeles"
    )
    assert result["returns"]["status"] == "ready"
    assert result["returns"]["observing_orders"] == 0
    assert result["returns"]["unmatched_records"] == 0
    assert result["returns"]["current"]["denominator"] == 1


def test_low_sample_observation_is_separate_from_events(metric_scope):
    from insight.metrics.observations import record_observation_leads
    from insight.models import MetricSnapshot, ObservationLead

    users, shops = metric_scope
    snapshot = MetricSnapshot.objects.create(
        shop=shops[0],
        child_asin="B000TEST01",
        actor=users["admin"],
        fingerprint="observation-test",
        rule_version="test",
        source_manifest={},
        local_date=date(2026, 9, 1),
        payload={
            "cohort": {
                "refunds": {
                    "cutoff": "2026-08-01",
                    "status": "ready",
                    "observation": {"status": "low_sample", "mature_orders": 2},
                    "reason": "样本不足",
                }
            }
        },
    )
    record_observation_leads(snapshot)
    assert ObservationLead.objects.count() == 1
    lead = ObservationLead.objects.get()
    assert lead.status == "open" and lead.metric == "cohort_refunds"
    record_observation_leads(snapshot)
    assert ObservationLead.objects.count() == 1
