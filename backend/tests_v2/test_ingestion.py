from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.exceptions import ValidationError

from insight.ingestion.service import accept_upload, create_source, parse_batch, publish_batch
from insight.models import AuditEntry, CoveragePartition, ImportBatch, SalesTrafficDaily
from insight.services import create_product

pytestmark = pytest.mark.django_db


@pytest.fixture
def source(setup_scope, settings, tmp_path):
    from datetime import date

    settings.STORAGE_ROOT = tmp_path
    users, shops = setup_scope
    for shop in shops:
        create_product(
            users["admin"],
            shop,
            {
                "child_asin": "B000TEST01",
                "seller_sku": "S1",
                "fulfillment": "FBA",
                "name": "测试商品",
                "parent_asin": "",
                "owner": None,
                "effective_from": date(2026, 1, 1),
                "reason": "测试",
            },
            "test",
        )
    source = create_source(
        users["admin"],
        shops[0],
        {
            "name": "销售",
            "kind": "sales_traffic",
            "is_primary": True,
            "grain": "asin_day",
            "column_mapping": {
                "child_asin": "asin",
                "business_date": "date",
                "ordered_units": "units",
                "sessions": "sessions",
            },
            "ignored_columns": [],
        },
        "test",
    )
    return users, shops, source


def upload(user, source, raw=None):
    from datetime import date

    raw = raw or b"asin,date,units,sessions\nB000TEST01,2026-09-01,10,100\n"
    return accept_upload(
        user,
        source,
        SimpleUploadedFile("sales.csv", raw),
        {
            "coverage_start": date(2026, 9, 1),
            "coverage_end": date(2026, 9, 2),
            "generated_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
            "declared_complete": True,
            "declared_asins": ["B000TEST01"],
        },
        "test",
    )


def publish(user, batch, partial=False):
    return publish_batch(user, batch.pk, allow_partial=partial, confirm_contract=True, request_id="test")


def test_duplicate_upload_and_publish_are_idempotent(source):
    users, shops, source = source
    first = upload(users["admin"], source)
    assert first.pk == upload(users["admin"], source).pk
    assert ImportBatch.objects.count() == 1
    parse_batch(first.pk, users["admin"])
    assert SalesTrafficDaily.objects.count() == 0
    publish(users["admin"], first)
    publish(users["admin"], first)
    assert SalesTrafficDaily.objects.count() == 1
    fact = SalesTrafficDaily.objects.get()
    assert fact.raw_row.line == 2 and fact.ordered_units == 10
    assert fact.product_id is None  # ASIN traffic must not be copied to each SKU.
    assert CoveragePartition.objects.get().child_asins == ["B000TEST01"]
    assert AuditEntry.objects.filter(action="import.published").count() == 1
    assert CoveragePartition.objects.get().status == "partial"  # second declared day has no row


def test_directory_requires_two_observations_and_rejects_symlinks(source, tmp_path):
    import json
    from datetime import timedelta

    from django.utils import timezone as django_timezone

    from insight.ingestion.directory import scan_directory
    from insight.models import DirectorySource

    users, _, data_source = source
    root = tmp_path / "inbox"
    root.mkdir()
    report = root / "daily.csv"
    report.write_bytes(b"asin,date,units,sessions\nB000TEST01,2026-09-01,10,100\n")
    (root / "daily.csv.json").write_text(
        json.dumps(
            {
                "coverage_start": "2026-09-01",
                "coverage_end": "2026-09-01",
                "generated_at": "2026-09-02T00:00:00Z",
                "declared_complete": True,
                "declared_asins": ["B000TEST01"],
            }
        )
    )
    outside = tmp_path / "outside.csv"
    outside.write_text("never read this")
    (root / "escape.csv").symlink_to(outside)
    directory = DirectorySource.objects.create(source=data_source, actor=users["admin"], root=str(root))
    scan_directory(directory.pk)
    assert ImportBatch.objects.count() == 0
    directory.refresh_from_db()
    assert "不允许" in directory.error
    directory.observations["daily.csv"]["observed_at"] -= 10
    directory.next_scan_at = django_timezone.now() - timedelta(seconds=1)
    directory.save()
    scan_directory(directory.pk)
    assert ImportBatch.objects.count() == 1
    assert ImportBatch.objects.get().filename == "daily.csv"


def test_partial_requires_explicit_approval_and_preserves_error_rows(source):
    users, _, source = source
    batch = upload(
        users["admin"],
        source,
        b"asin,date,units,sessions\nB000TEST01,2026-09-01,10,100\nB000TEST01,2026-09-02,not-a-number,100\n",
    )
    parse_batch(batch.pk, users["admin"])
    batch.refresh_from_db()
    assert (batch.valid_rows, batch.error_rows) == (1, 1)
    with pytest.raises(ValidationError):
        publish(users["admin"], batch)
    assert SalesTrafficDaily.objects.count() == 0
    publish(users["admin"], batch, partial=True)
    assert CoveragePartition.objects.get().status == "partial"
    assert batch.rows.get(line=3).raw["units"] == "not-a-number"


def test_corrections_keep_previous_fact_and_publication_is_atomic(source):
    users, _, source = source
    first = upload(users["admin"], source)
    parse_batch(first.pk, users["admin"])
    publish(users["admin"], first)
    second = upload(users["admin"], source, b"asin,date,units,sessions\nB000TEST01,2026-09-01,12,100\n")
    parse_batch(second.pk, users["admin"])
    with patch.object(
        SalesTrafficDaily.objects, "bulk_create", side_effect=RuntimeError("simulated database failure")
    ):
        with pytest.raises(RuntimeError):
            publish(users["admin"], second)
    source.refresh_from_db()
    assert source.revision == 1 and SalesTrafficDaily.objects.count() == 1
    publish(users["admin"], second)
    assert list(SalesTrafficDaily.objects.order_by("revision").values_list("ordered_units", flat=True)) == [
        10,
        12,
    ]
    assert SalesTrafficDaily.objects.values("business_key").distinct().count() == 1


def test_schema_change_duplicates_and_empty_files_do_not_publish(source):
    users, _, source = source
    for raw in [
        b"asin,date,units,changed\nB000TEST01,2026-09-01,10,100\n",
        b"asin,date,units,sessions\n",
        b"asin,date,units,sessions\nB000TEST01,2026-09-01,10,100\nB000TEST01,2026-09-01,11,100\n",
    ]:
        batch = upload(users["admin"], source, raw)
        parse_batch(batch.pk, users["admin"])
        batch.refresh_from_db()
        assert batch.status == "invalid"
    assert SalesTrafficDaily.objects.count() == 0


def test_import_api_hides_other_shop_and_contract_confirm_required(client, source):
    users, _, source = source
    batch = upload(users["admin"], source)
    parse_batch(batch.pk, users["admin"])
    client.force_authenticate(users["b"])
    assert client.get(f"/api/v1/imports/{batch.pk}/").status_code == 404
    assert client.get(f"/api/v1/imports/{batch.pk}/rows/").status_code == 404
    with pytest.raises(ValidationError):
        publish_batch(
            users["admin"], batch.pk, allow_partial=False, confirm_contract=False, request_id="test"
        )


@pytest.mark.django_db(transaction=True)
def test_original_file_download_is_scoped_and_audited(client, source):
    users, _, data_source = source
    batch = upload(users["admin"], data_source)
    client.force_authenticate(users["b"])
    assert client.get(f"/api/v1/imports/{batch.pk}/original-file/").status_code == 404
    client.force_authenticate(users["admin"])
    response = client.get(f"/api/v1/imports/{batch.pk}/original-file/")
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"asin,date,units,sessions\nB000TEST01,2026-09-01,10,100\n"
    response.close()
    assert (
        AuditEntry.objects.filter(action="import.original_downloaded", target_id=str(batch.pk)).count() == 1
    )


def test_directory_mutation_during_read_rolls_back_import(source, tmp_path):
    import json

    from insight.ingestion.directory import ingest_stable_file
    from insight.models import DirectorySource, Job

    users, _, data_source = source
    report = tmp_path / "changing.csv"
    report.write_bytes(b"asin,date,units,sessions\nB000TEST01,2026-09-01,10,100\n")
    meta = tmp_path / "changing.csv.json"
    meta.write_text(
        json.dumps(
            {
                "coverage_start": "2026-09-01",
                "coverage_end": "2026-09-01",
                "generated_at": "2026-09-02T00:00:00Z",
                "declared_asins": ["B000TEST01"],
            }
        )
    )
    directory = DirectorySource.objects.create(source=data_source, actor=users["admin"], root=str(tmp_path))
    a, b = report.stat(), meta.stat()
    fingerprint = [a.st_size, a.st_mtime_ns, b.st_size, b.st_mtime_ns]

    def changed(*args, **kwargs):
        batch = accept_upload(*args, **kwargs)
        with report.open("ab") as stream:
            stream.write(b"changed while reading\n")
        return batch

    with patch("insight.ingestion.directory.accept_upload", side_effect=changed):
        with pytest.raises(ValueError, match="发生变化"):
            ingest_stable_file(directory, report, meta, fingerprint)
    assert not ImportBatch.objects.exists()
    assert not Job.objects.exists()


def test_empty_event_report_requires_explicit_zero_contract(source):
    from datetime import date

    from insight.models import RefundTransaction

    users, shops, _ = source
    mapping = {
        key: key
        for key in [
            "child_asin",
            "seller_sku",
            "fulfillment",
            "business_date",
            "record_id",
            "order_line_id",
            "transaction_chain_id",
            "amount",
            "currency",
        ]
    }
    declared = create_source(
        users["admin"],
        shops[0],
        {
            "name": "明确零事件退款源",
            "kind": "refunds",
            "is_primary": True,
            "grain": "product_record",
            "column_mapping": mapping,
            "ignored_columns": [],
            "empty_report_means_zero": True,
        },
        "test",
    )
    batch = accept_upload(
        users["admin"],
        declared,
        SimpleUploadedFile("empty-refunds.csv", (",".join(mapping) + "\n").encode()),
        {
            "coverage_start": date(2026, 9, 1),
            "coverage_end": date(2026, 9, 2),
            "generated_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
            "declared_complete": True,
            "declared_asins": ["B000TEST01"],
        },
        "test",
    )
    parse_batch(batch.pk, users["admin"])
    batch.refresh_from_db()
    assert batch.status == "preview" and batch.total_rows == 0
    publish(users["admin"], batch)
    assert CoveragePartition.objects.get(batch=batch).status == "ready"
    assert not RefundTransaction.objects.exists()
