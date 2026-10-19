import csv
import importlib.util
import json
from datetime import date, datetime, timedelta
from datetime import timezone as datetime_timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import CommandError
from django.utils import timezone

from insight.management.commands._acceptance import require_isolated_acceptance_environment
from insight.models import (
    ActionExecution,
    ActionTask,
    BusinessEvent,
    ImportBatch,
    InvestigationRun,
    Job,
    OrderLine,
    Product,
    ProductMapping,
    SalesTrafficDaily,
    Source,
    SupportCase,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPOSITORY_ROOT / "dataset" / "data"
LOOP_ROOT = DATA_ROOT / "05_近期经营闭环_v1"
CAPACITY_ROOT = DATA_ROOT / "08_S7容量评测_v1"
CAPACITY_PREPARE_PATH = REPOSITORY_ROOT / "dataset" / "prepare_capacity_acceptance.py"
CAPACITY_PREPARE_SPEC = importlib.util.spec_from_file_location("capacity_acceptance_prepare", CAPACITY_PREPARE_PATH)
assert CAPACITY_PREPARE_SPEC and CAPACITY_PREPARE_SPEC.loader
CAPACITY_PREPARE = importlib.util.module_from_spec(CAPACITY_PREPARE_SPEC)
CAPACITY_PREPARE_SPEC.loader.exec_module(CAPACITY_PREPARE)


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        yield from csv.DictReader(source)


def create_products(shop, asins, *, skus_per_asin=2):
    products = []
    mappings = []
    for asin in asins:
        for index in range(1, skus_per_asin + 1):
            product = Product(
                shop=shop,
                child_asin=asin,
                seller_sku=f"{asin}-SKU{index}" if skus_per_asin == 2 else f"{asin}-CAP",
                fulfillment="FBA" if index == 1 else "FBM",
            )
            products.append(product)
    Product.objects.bulk_create(products, batch_size=500)
    for product in Product.objects.filter(shop=shop, child_asin__in=asins):
        mappings.append(ProductMapping(
            product=product,
            name=f"Synthetic acceptance fixture {product.child_asin} {product.seller_sku}",
            effective_from=date(2020, 1, 1),
            version=1,
            reason="isolated extended acceptance fixture",
        ))
    ProductMapping.objects.bulk_create(mappings, batch_size=500)
    return {product.child_asin: product for product in Product.objects.filter(shop=shop, child_asin__in=asins)}


def test_capacity_import_credentials_path_is_explicit_and_repo_relative(tmp_path):
    assert CAPACITY_PREPARE.credential_output_path() == (
        REPOSITORY_ROOT / "backend/.local/s7_capacity_acceptance_credentials.json"
    )
    assert CAPACITY_PREPARE.credential_output_path(Path(".local/capacity-run.json")) == (
        REPOSITORY_ROOT / ".local/capacity-run.json"
    )
    with pytest.raises(ValueError, match="stored under repository .local"):
        CAPACITY_PREPARE.credential_output_path(tmp_path / "private" / "capacity-run.json")


@pytest.mark.parametrize(
    ("database_name", "accepted"),
    [
        ("insight_acceptance", True),
        ("insight_acceptance_20261005", True),
        ("insight_acceptance_20261006_v2", True),
        ("insight_acceptance_dev", False),
    ],
)
def test_acceptance_environment_only_allows_base_or_date_scoped_database(
    monkeypatch, database_name, accepted
):
    repository_root = Path(__file__).resolve().parents[2]
    default_database = {
        **settings.DATABASES["default"],
        "NAME": database_name,
        "HOST": "127.0.0.1",
    }
    monkeypatch.setattr(settings, "DEBUG", True)
    monkeypatch.setattr(settings, "DATABASES", {"default": default_database})
    monkeypatch.setattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(
        settings,
        "STORAGE_ROOT",
        repository_root / ".local" / "acceptance-storage" / database_name,
    )

    monkeypatch.setattr(settings, "ACCEPTANCE_ENVIRONMENT", database_name)
    monkeypatch.setattr(settings, "CELERY_NAMESPACE", database_name)
    monkeypatch.setattr(settings, "CELERY_TASK_DEFAULT_QUEUE", database_name)
    monkeypatch.setattr(settings, "CELERY_TASK_ROUTES", {"*": {"queue": database_name}})
    monkeypatch.setattr(settings, "CELERY_BROKER_TRANSPORT_OPTIONS", {"global_keyprefix": database_name + ":"})
    monkeypatch.setattr(settings, "CELERY_BEAT_SCHEDULE_FILENAME", str(repository_root / ".local" / "acceptance-artifacts" / "guard-test" / f"beat-{database_name}.db"))
    if accepted:
        details = require_isolated_acceptance_environment()
        assert details["database"] == database_name
        assert details["storage"] == "local-acceptance"
        for field, wrong in [
            ("CELERY_NAMESPACE", "celery"),
            ("CELERY_TASK_ROUTES", {}),
            ("CELERY_BROKER_TRANSPORT_OPTIONS", {}),
            ("STORAGE_ROOT", repository_root / ".local" / "acceptance-storage"),
            ("CELERY_BEAT_SCHEDULE_FILENAME", "celerybeat-schedule"),
        ]:
            with monkeypatch.context() as changed:
                changed.setattr(settings, field, wrong)
                with pytest.raises(CommandError):
                    require_isolated_acceptance_environment()
    else:
        with pytest.raises(CommandError, match="date-isolated"):
            require_isolated_acceptance_environment()


def run_queued_job(job_id):
    from insight.tasks import run_job

    run_job(str(job_id))
    return Job.objects.get(pk=job_id)


def upload_package_file(client, user, shop, path, source_by_kind, *, sidecar_override=None):
    sidecar_path = Path(str(path) + ".meta.json")
    meta = sidecar_override or json.loads(sidecar_path.read_text(encoding="utf-8"))
    source_payload = dict(meta["source_payload"])
    source_payload["shop"] = str(shop.pk)
    kind = source_payload["kind"]
    source = source_by_kind.get(kind)
    if source is None:
        created = client.post("/api/v1/sources/", source_payload, format="json")
        assert created.status_code == 201, created.data
        source = Source.objects.get(pk=created.data["id"])
        source_by_kind[kind] = source

    upload = SimpleUploadedFile(path.name, path.read_bytes(), content_type="text/csv")
    upload_payload = dict(meta["upload_payload"])
    upload_payload["file"] = upload
    accepted = client.post(
        f"/api/v1/sources/{source.pk}/upload/",
        upload_payload,
        format="multipart",
    )
    assert accepted.status_code == 202, accepted.data
    batch = ImportBatch.objects.get(pk=accepted.data["id"])
    parse_job = Job.objects.get(kind="parse_import", idempotency_key=str(batch.pk))
    parse_job = run_queued_job(parse_job.pk)
    batch.refresh_from_db()
    assert parse_job.status == "completed", (parse_job.status, parse_job.error)
    assert batch.status == "preview", (batch.status, batch.error, list(batch.rows.exclude(error="").values_list("error", flat=True)[:5]))
    assert batch.error_rows == 0

    published = client.post(
        f"/api/v1/imports/{batch.pk}/publish/",
        {"allow_partial": False, "confirm_contract": True},
        format="json",
    )
    assert published.status_code == 202, published.data
    publish_job = run_queued_job(published.data["id"])
    batch.refresh_from_db()
    assert publish_job.status == "completed", (publish_job.status, publish_job.error)
    assert batch.status == "published"
    return batch, source


def scan_through_api(client, shop, asin, request_key):
    submitted = client.post(
        "/api/v1/snapshots/scan/",
        {"shop": str(shop.pk), "child_asin": asin, "idempotency_key": request_key},
        format="json",
    )
    assert submitted.status_code == 202, submitted.data
    job = run_queued_job(submitted.data["id"])
    assert job.status == "completed", (job.status, job.error, job.result)
    return job.result["snapshot_id"]


def create_and_complete_action(client, shop, user, scenario, snapshot_id, fixed_now, *, event_id=None):
    effective_at = datetime.combine(
        date.fromisoformat(scenario["effective_date"]), datetime.min.time(), ZoneInfo(shop.timezone)
    ).replace(hour=12)
    created = client.post(
        "/api/v1/action-tasks/",
        {
            "shop": str(shop.pk),
            "child_asin": scenario["asin"],
            **({"event": str(event_id)} if event_id else {}),
            "title": f"API 验收：{scenario['scenario_id']}",
            "assigned_to": user.pk,
            "due_at": (fixed_now + timedelta(days=2)).isoformat(),
            "question": "动作生效后的完整窗口是否出现预期变化？",
            "completion_criteria": "比较已发布事实，仅记录变化，不宣称因果。",
            "observed_metrics": [scenario["metric"]],
            "evidence_refs": [f"snapshot:{snapshot_id}"],
            "is_formal": True,
        },
        format="json",
    )
    assert created.status_code == 201, created.data
    completed = client.post(
        f"/api/v1/action-tasks/{created.data['id']}/complete/",
        {
            "expected_version": created.data["version"],
            "notes": "合成数据 API 验收动作。",
            "effective_at": effective_at.isoformat(),
            "review_at": fixed_now.isoformat(),
            "observed_metrics": [scenario["metric"]],
            "evidence_refs": [f"snapshot:{snapshot_id}"],
        },
        format="json",
    )
    assert completed.status_code == 200, completed.data
    assert ActionExecution.objects.filter(task_id=created.data["id"]).exists()
    return created, completed


def expected_daily_action_values(sales_rows, asin, effective_day):
    baseline_days = [effective_day - timedelta(days=offset) for offset in range(28, 0, -1)]
    observation_days = [effective_day + timedelta(days=offset) for offset in range(1, 8)]
    by_key = {(row["child_asin"], date.fromisoformat(row["business_date"])): row for row in sales_rows}
    baseline_total = sum(int(by_key[(asin, day)]["ordered_units"]) for day in baseline_days)
    current_total = sum(int(by_key[(asin, day)]["ordered_units"]) for day in observation_days)

    def daily_mean(total, days):
        value = (Decimal(total) / Decimal(days)).quantize(Decimal("0.000001")).normalize()
        return format(value, "f")

    baseline, current = daily_mean(baseline_total, 28), daily_mean(current_total, 7)
    delta = format((Decimal(current) - Decimal(baseline)).normalize(), "f")
    return {
        "baseline_total": str(baseline_total),
        "current_total": str(current_total),
        "baseline": baseline,
        "current": current,
        "delta": delta,
        "baseline_start": str(baseline_days[0]),
        "baseline_end": str(baseline_days[-1]),
        "current_start": str(observation_days[0]),
        "current_end": str(observation_days[-1]),
    }


@pytest.mark.django_db
@pytest.mark.integration
def test_recent_loop_imports_and_drives_investigation_action_review_and_recurrence(
    setup_scope, settings, tmp_path, client, monkeypatch
):
    settings.STORAGE_ROOT = tmp_path
    users, shops = setup_scope
    shop = shops[0]
    manifest = json.loads((LOOP_ROOT / "manifest.json").read_text(encoding="utf-8"))
    asins = manifest["source_population"]["complete_asins"]
    action_scenarios = list(csv_rows(DATA_ROOT / "06_S5动作效果场景_v1" / "scenario_catalog.csv"))
    missing_asin = next(row["asin"] for row in action_scenarios if row["scenario_id"] == "missing_data")
    create_products(shop, [*asins, missing_asin])
    client.force_authenticate(users["admin"])

    sources = {}
    batches = []
    for kind in ("sales_traffic", "orders", "refunds", "returns", "inventory", "ratings", "operations", "reviews", "support"):
        info = manifest["files"][kind]
        batch, _ = upload_package_file(client, users["admin"], shop, LOOP_ROOT / info["path"], sources)
        batches.append(batch)
    gap_info = manifest["files"]["gap_fixture"]
    gap_batch, _ = upload_package_file(client, users["admin"], shop, LOOP_ROOT / gap_info["path"], sources)
    batches.append(gap_batch)
    assert sum(batch.total_rows for batch in batches) == manifest["totals"]["complete_fact_rows"] + gap_info["rows"]

    fixed_now = datetime(2026, 10, 1, 20, 0, tzinfo=datetime_timezone.utc)
    monkeypatch.setattr(timezone, "now", lambda: fixed_now)
    sales_rows = list(csv_rows(LOOP_ROOT / manifest["files"]["sales_traffic"]["path"]))
    scenarios_by_id = {row["scenario_id"]: row for row in action_scenarios}
    for scenario_id in ("improved_after_action", "no_improvement", "worsening", "window_not_mature", "missing_data"):
        scenario = scenarios_by_id[scenario_id]
        scenario_snapshot = scan_through_api(
            client, shop, scenario["asin"], f"extended-action-{scenario_id}-scan"
        )
        created, completed_action = create_and_complete_action(
            client, shop, users["admin"], scenario, scenario_snapshot, fixed_now
        )
        reviewed_action = client.post(
            f"/api/v1/action-tasks/{created.data['id']}/review/",
            {
                "expected_version": completed_action.data["version"],
                "status": "completed",
                "result": f"合成场景 {scenario_id} 的已知答案 API 复核。",
                "review_at": fixed_now.isoformat(),
                "snapshot": scenario_snapshot,
            },
            format="json",
        )
        if scenario["expected_outcome"] == "not_reviewable":
            assert reviewed_action.status_code == 400, (scenario_id, reviewed_action.data)
            expected_reason = {
                "observation_window_not_mature": "完整站点日",
                "coverage_gap": "缺报日期",
            }[scenario["expected_stop_reason"]]
            assert expected_reason in " ".join(map(str, reviewed_action.data["field_errors"])), (
                scenario_id,
                reviewed_action.data,
            )
            created_action = ActionTask.objects.get(pk=created.data["id"])
            assert created_action.review_status == "scheduled"
            assert created_action.review_comparison == {}
            continue

        assert reviewed_action.status_code == 200, (scenario_id, reviewed_action.data)
        effective_day = date.fromisoformat(scenario["effective_date"])
        expected = expected_daily_action_values(sales_rows, scenario["asin"], effective_day)
        actual = reviewed_action.data["review_comparison"]["ordered_units"]
        for key in ("baseline_total", "current_total", "baseline", "current", "delta"):
            assert actual[key] == expected[key], (scenario_id, key, actual[key], expected[key])
        assert actual["baseline_days"] == 28 and actual["current_days"] == 7
        assert actual["unit"] == "units/day" and actual["basis"] == "daily_mean"
        assert actual["window_start"] == expected["current_start"]
        assert actual["window_end"] == expected["current_end"]
        window_evidence = actual["window_evidence"]
        assert window_evidence["baseline"]["start"] == expected["baseline_start"]
        assert window_evidence["baseline"]["end"] == expected["baseline_end"]
        assert len(window_evidence["baseline"]["raw_row_ids"]) == 28
        assert len(window_evidence["current"]["raw_row_ids"]) == 7
        if scenario["expected_outcome"] == "improved":
            assert Decimal(actual["delta"]) > 0
        elif scenario["expected_outcome"] == "no_material_change":
            assert Decimal(actual["delta"]) == 0
        elif scenario["expected_outcome"] == "worsened":
            assert Decimal(actual["delta"]) < 0
        assert ActionTask.objects.get(pk=created.data["id"]).review_comparison["ordered_units"]["window_evidence"]["current"]["raw_row_ids"]

    recurrence_asin = scenarios_by_id["closed_then_recurrence"]["asin"]
    snapshot_id = scan_through_api(client, shop, recurrence_asin, "extended-loop-first-scan")
    event = BusinessEvent.objects.get(shop=shop, child_asin=recurrence_asin, family="sales")
    assert event.evidence.filter(snapshot_id=snapshot_id).exists()

    investigation_created = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shop.pk),
            "child_asin": recurrence_asin,
            "query": f"调查 {recurrence_asin} 的销量变化并核查证据",
            "planner_provider": "deterministic",
            "max_steps": 6,
            "idempotency_key": "extended-loop-investigation",
        },
        format="json",
    )
    assert investigation_created.status_code == 202, investigation_created.data
    investigation_job = run_queued_job(investigation_created.data["id"])
    assert investigation_job.status == "completed", (investigation_job.status, investigation_job.error)
    investigation = InvestigationRun.objects.get(pk=investigation_job.result["investigation_id"])
    assert investigation.status in {"completed", "partial", "needs_clarification"}
    assert investigation.state["planner_provider"] == "deterministic"
    assert investigation.model_attempts == 0 and investigation.model_cost == 0

    task_created = client.post(
        "/api/v1/action-tasks/",
        {
            "shop": str(shop.pk),
            "child_asin": recurrence_asin,
            "event": str(event.pk),
            "title": "核对合成销量复发场景",
            "assigned_to": users["admin"].pk,
            "due_at": "2026-10-03T20:00:00Z",
            "question": "生效后销量是否有变化？",
            "completion_criteria": "引用完整观察窗口，只记录方向，不声称因果。",
            "observed_metrics": ["ordered_units"],
            "evidence_refs": [f"snapshot:{snapshot_id}"],
            "is_formal": True,
        },
        format="json",
    )
    assert task_created.status_code == 201, task_created.data
    task_id = task_created.data["id"]
    effective_at = datetime(2026, 9, 23, 12, 0, tzinfo=ZoneInfo(shop.timezone)).isoformat()
    completed = client.post(
        f"/api/v1/action-tasks/{task_id}/complete/",
        {
            "expected_version": task_created.data["version"],
            "notes": "合成验收动作已经执行。",
            "effective_at": effective_at,
            "review_at": fixed_now.isoformat(),
            "observed_metrics": ["ordered_units"],
            "evidence_refs": [f"snapshot:{snapshot_id}"],
        },
        format="json",
    )
    assert completed.status_code == 200, completed.data
    assert ActionExecution.objects.filter(task_id=task_id).exists()

    event.refresh_from_db()
    claimed = client.post(
        f"/api/v1/events/{event.pk}/command/",
        {"expected_version": event.version, "operation": "claim", "reason": "隔离验收负责人领取"},
        format="json",
    )
    assert claimed.status_code == 200, claimed.data
    event.refresh_from_db()
    observing = client.post(
        f"/api/v1/events/{event.pk}/command/",
        {
            "expected_version": event.version,
            "operation": "observe",
            "action_task": task_id,
            "reason": "动作已执行，等待窗口对比",
        },
        format="json",
    )
    assert observing.status_code == 200, observing.data

    reviewed = client.post(
        f"/api/v1/action-tasks/{task_id}/review/",
        {
            "expected_version": completed.data["version"],
            "status": "completed",
            "result": "合成完整窗口已比较；该变化不构成因果结论。",
            "review_at": fixed_now.isoformat(),
            "snapshot": snapshot_id,
        },
        format="json",
    )
    assert reviewed.status_code == 200, reviewed.data
    recurrence_scenario = scenarios_by_id["closed_then_recurrence"]
    recurrence_expected = expected_daily_action_values(
        sales_rows,
        recurrence_asin,
        date.fromisoformat(recurrence_scenario["effective_date"]),
    )
    recurrence_comparison = reviewed.data["review_comparison"]["ordered_units"]
    assert recurrence_comparison["basis"] == "daily_mean"
    for key in ("baseline_total", "current_total", "baseline", "current", "delta"):
        assert recurrence_comparison[key] == recurrence_expected[key], (key, recurrence_comparison[key], recurrence_expected[key])
    assert recurrence_comparison["window_start"] == recurrence_expected["current_start"]
    assert recurrence_comparison["window_end"] == recurrence_expected["current_end"]
    assert recurrence_comparison["baseline_days"] == 28 and recurrence_comparison["current_days"] == 7

    event.refresh_from_db()
    closed = client.post(
        f"/api/v1/events/{event.pk}/command/",
        {
            "expected_version": event.version,
            "operation": "close",
            "closure_reason": "known_change",
            "reason": "完成合成场景复查并记录已知范围",
            "evidence_refs": [snapshot_id],
        },
        format="json",
    )
    assert closed.status_code == 200, closed.data
    event.refresh_from_db()
    assert event.status == "closed"

    recurrence_info = manifest["files"]["recurrence_fixture"]
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 10, 8, 20, 0, tzinfo=datetime_timezone.utc))
    for info in recurrence_info:
        batch, _ = upload_package_file(client, users["admin"], shop, LOOP_ROOT / info["path"], sources)
        assert batch.total_rows == info["rows"]
    next_snapshot = scan_through_api(client, shop, recurrence_asin, "extended-loop-recurrence-scan")
    event.refresh_from_db()
    assert event.status == "pending"
    assert event.evidence.filter(snapshot_id=next_snapshot, detail__metric="sales_units").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.capacity
def test_capacity_package_imports_all_502400_rows_through_ingestion_api(setup_scope, settings, tmp_path, client):
    settings.STORAGE_ROOT = tmp_path
    users, shops = setup_scope
    client.force_authenticate(users["admin"])
    manifest = json.loads((CAPACITY_ROOT / "manifest.json").read_text(encoding="utf-8"))
    batches = []

    for shop, slot in zip(shops, ("shop_a", "shop_b"), strict=True):
        first_asin = next(csv_rows(CAPACITY_ROOT / "import_ready" / slot / "sales_traffic_v1.csv"))["child_asin"]
        asins = sorted({row["child_asin"] for row in csv_rows(CAPACITY_ROOT / "import_ready" / slot / "sales_traffic_v1.csv")})
        create_products(shop, asins, skus_per_asin=1)
        sources = {}
        for filename in ("sales_traffic_v1.csv", "orders_v1.csv", "support_cases_v1.csv", "sales_traffic_revision_v2.csv"):
            path = CAPACITY_ROOT / "import_ready" / slot / filename
            if filename == "orders_v1.csv":
                sidecar = json.loads(Path(str(path) + ".meta.json").read_text(encoding="utf-8"))
                parts = CAPACITY_PREPARE.split_order_file(path, sidecar, tmp_path / f"{slot}-order-shards", window_days=30)
                assert len(parts) == 6
                for part in parts:
                    batch, _ = upload_package_file(
                        client,
                        users["admin"],
                        shop,
                        part["path"],
                        sources,
                        sidecar_override=part["sidecar"],
                    )
                    batches.append(batch)
                assert sum(part["rows"] for part in parts) == manifest["files"][slot]["orders_v1.csv"]
                continue
            batch, _ = upload_package_file(
                client,
                users["admin"],
                shop,
                path,
                sources,
            )
            batches.append(batch)
        assert len(asins) == 500 and first_asin in asins

    assert sum(batch.total_rows for batch in batches) == manifest["actual_counts"]["total_fact_rows"] == 502_400
    assert all(batch.error_rows == 0 and batch.status == "published" for batch in batches)
    assert OrderLine.objects.count() == manifest["actual_counts"]["order_lines"]
    assert SupportCase.objects.count() == manifest["actual_counts"]["support_cases"]
    assert SalesTrafficDaily.objects.count() == manifest["actual_counts"]["daily_sales_rows"] + manifest["actual_counts"]["version_2_corrections"]
    assert all(
        Source.objects.get(shop=shop, kind="sales_traffic", is_primary=True).revision == 2
        for shop in shops
    )
