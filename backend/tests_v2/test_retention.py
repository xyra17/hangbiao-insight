from datetime import UTC, date, datetime, timedelta

import pytest
from django.utils import timezone

from insight.models import ImportBatch, RawFile, RetentionTombstone, Source, SourceContract
from insight.retention import cleanup_retention

pytestmark = pytest.mark.django_db


def test_retention_removes_raw_payload_but_keeps_tombstone(metric_scope, settings, tmp_path):
    users, shops = metric_scope
    settings.STORAGE_ROOT = tmp_path
    path = tmp_path / "raw-1"
    path.write_text("sensitive")
    raw = RawFile.objects.create(sha256="a" * 64, storage_key="raw-1", size=9)
    old = timezone.now() - timedelta(days=181)
    RawFile.objects.filter(pk=raw.pk).update(created_at=old)
    source = Source.objects.create(shop=shops[0], name="测试来源", kind="sales_traffic", is_primary=True)
    contract = SourceContract.objects.create(
        source=source, version=1, grain="asin_day", column_mapping={"child_asin": "asin", "business_date": "date"},
        retention_days=180,
    )
    batch = ImportBatch.objects.create(
        contract=contract, raw_file=raw, actor=users["admin"], filename="raw.csv", processing_key="p" * 64,
        coverage_start=date(2026, 1, 1), coverage_end=date(2026, 1, 1), generated_at=datetime(2026, 1, 2, tzinfo=UTC),
        declared_asins=["B000TEST01"],
    )
    ImportBatch.objects.filter(pk=batch.pk).update(created_at=old)
    result = cleanup_retention(now=timezone.now(), workspace_id=shops[0].workspace_id, actor=users["admin"])
    assert result["raw_files"] == 1
    assert not path.exists()
    raw.refresh_from_db()
    assert raw.deleted_at is not None and raw.storage_key == ""
    assert RetentionTombstone.objects.filter(object_id=str(raw.pk), object_type="raw_file").exists()


def test_retention_cleanup_is_scheduled_independently_from_daily_scans(settings):
    schedule = settings.CELERY_BEAT_SCHEDULE["retention-cleanup"]
    assert schedule["task"] == "insight.tasks.cleanup_retention_task"
    assert schedule["schedule"] == 86400.0


def test_retention_task_delegates_to_domain_cleanup(monkeypatch):
    from insight import tasks

    expected = {"dry_run": False, "raw_files": 0}
    called = []

    def fake_cleanup():
        called.append(True)
        return expected

    monkeypatch.setattr("insight.retention.cleanup_retention", fake_cleanup)
    assert tasks.cleanup_retention_task() == expected
    assert called == [True]
