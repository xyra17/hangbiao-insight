from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.exceptions import PermissionDenied

from insight.ingestion.service import accept_upload, parse_batch, publish_batch
from insight.metrics.facts import coverage_days, latest_facts, source_manifest
from insight.metrics.occurrence import calculate_occurrences
from insight.models import CoveragePartition, Membership, StagedRow
from tests_v2.test_ingestion_boundaries import import_rows, source_sample

pytestmark = pytest.mark.django_db


def upload(actor, source, content):
    return accept_upload(
        actor,
        source,
        SimpleUploadedFile("edge.csv", content),
        {
            "coverage_start": date(2026, 9, 1),
            "coverage_end": date(2026, 9, 1),
            "generated_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
            "declared_complete": True,
            "declared_asins": ["B000TEST01"],
        },
        "parse-recovery",
    )


@pytest.mark.parametrize(
    "content",
    [
        b"child_asin,business_date,ordered_units,sessions\nB000TEST01,2026-09-01,10,\xff\n",
        b"child_asin,business_date,ordered_units,sessions\nB000TEST01,2026-09-01,10,"
        + b"1" * (512 * 1024 + 1)
        + b"\n",
    ],
)
def test_invalid_encoding_and_oversized_field_cannot_publish(metric_scope, content):
    users, _, source, _ = source_sample(metric_scope, "sales_traffic")
    batch = upload(users["admin"], source, content)
    parse_batch(batch.pk, users["admin"])
    batch.refresh_from_db()
    assert batch.status == "invalid"
    with pytest.raises(Exception) as error:
        publish_batch(
            users["admin"], batch.pk, allow_partial=True, confirm_contract=True, request_id="invalid"
        )
    assert error.value.status_code == 409
    assert not CoveragePartition.objects.exists()
    source.refresh_from_db()
    assert source.revision == 0


def test_interrupted_parse_resumes_at_committed_chunk_without_duplicates(metric_scope):
    users, _, source, _ = source_sample(metric_scope, "reviews")
    content = (
        "child_asin,business_date,record_id,title,text,rating,language,collected_at,is_deleted\n"
        + "".join(
            f"B000TEST01,2026-09-01,R{i},Title,Review {i},3,en,2026-09-02T00:00:00Z,false\n" for i in range(5)
        )
    )
    batch = upload(users["admin"], source, content.encode())
    calls = 0

    def interrupt():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("worker interrupted")

    with patch("insight.ingestion.service.CHUNK_SIZE", 2):
        with pytest.raises(RuntimeError):
            parse_batch(batch.pk, users["admin"], interrupt)
        batch.refresh_from_db()
        assert batch.parse_cursor == 3 and batch.rows.count() == 2
        parse_batch(batch.pk, users["admin"])
    batch.refresh_from_db()
    assert batch.status == "preview" and batch.valid_rows == 5
    assert StagedRow.objects.filter(batch=batch).count() == 5
    publish_batch(
        users["admin"], batch.pk, allow_partial=False, confirm_contract=True, request_id="recovered"
    )
    assert latest_facts(source_manifest(source.shop), "reviews", "B000TEST01").count() == 5


def test_revocation_after_last_chunk_cannot_finalize_preview(metric_scope):
    from insight.ingestion.service import save_chunk

    users, _, source, _ = source_sample(metric_scope, "sales_traffic")
    batch = upload(
        users["admin"],
        source,
        b"child_asin,business_date,ordered_units,sessions\nB000TEST01,2026-09-01,10,100\n",
    )

    def revoke(*args, **kwargs):
        save_chunk(*args, **kwargs)
        Membership.objects.filter(user=users["admin"]).update(is_admin=False)

    with patch("insight.ingestion.service.save_chunk", side_effect=revoke):
        with pytest.raises(PermissionDenied):
            parse_batch(batch.pk, users["admin"])
    batch.refresh_from_db()
    assert batch.status == "parsing"
    assert not CoveragePartition.objects.exists()


def test_received_return_lifecycle_replaces_record_and_preserves_frozen_history(metric_scope):
    users, shops, source, row = source_sample(metric_scope, "returns")
    import_rows(users["admin"], source, [{**row, "status": "requested", "received_date": ""}])
    requested = source_manifest(shops[0])
    import_rows(users["admin"], source, [row])
    received = source_manifest(shops[0])
    import_rows(users["admin"], source, [{**row, "status": "cancelled"}])
    cancelled = source_manifest(shops[0])
    for manifest, expected in [(requested, 0), (received, 1), (cancelled, 0)]:
        assert latest_facts(manifest, "returns", "B000TEST01").count() == 1
        current = calculate_occurrences(manifest, "B000TEST01", date(2026, 9, 2))["returns"]["current"]
        assert current["event_count"] == expected
    assert latest_facts(received, "returns", "B000TEST01").get().status == "received"


def test_new_partial_coverage_supersedes_old_ready_without_mutating_snapshot(metric_scope):
    users, shops, source, row = source_sample(metric_scope, "sales_traffic")
    import_rows(users["admin"], source, [row])
    frozen = source_manifest(shops[0])
    batch = upload(
        users["admin"],
        source,
        b"child_asin,business_date,ordered_units,sessions\nB000TEST01,2026-09-01,20,100\n",
    )
    batch.declared_complete = False
    batch.save()
    parse_batch(batch.pk, users["admin"])
    publish_batch(users["admin"], batch.pk, allow_partial=False, confirm_contract=True, request_id="partial")
    assert coverage_days(frozen, "sales_traffic", "B000TEST01")[date(2026, 9, 1)] == "ready"
    assert (
        coverage_days(source_manifest(shops[0]), "sales_traffic", "B000TEST01")[date(2026, 9, 1)] == "partial"
    )


def test_requested_return_lifecycle_counts_request_then_cancellation(metric_scope):
    from insight.models import SourceContract
    users, shops, source, row = source_sample(metric_scope, "returns")
    contract = SourceContract.objects.get(source=source)
    contract.return_event = "requested"
    contract.column_mapping = {**contract.column_mapping, "requested_date": "requested_date"}
    contract.save()
    row = {**row, "requested_date": "2026-09-01"}
    import_rows(users["admin"], source, [{**row, "status":"requested", "received_date":""}])
    requested = source_manifest(shops[0])
    import_rows(users["admin"], source, [row])
    received = source_manifest(shops[0])
    import_rows(users["admin"], source, [{**row,"status":"cancelled"}])
    cancelled = source_manifest(shops[0])
    for manifest, expected in [(requested,1),(received,1),(cancelled,0)]:
        result = calculate_occurrences(manifest,"B000TEST01",date(2026,9,2))
        assert result["returns"]["current"]["event_count"] == expected


def test_unknown_return_status_is_traceable_but_not_counted(metric_scope):
    users, shops, source, row = source_sample(metric_scope, "returns")
    import_rows(users["admin"], source, [{**row, "status": "seller_specific_state", "received_date": ""}])
    result = calculate_occurrences(source_manifest(shops[0]), "B000TEST01", date(2026, 9, 2))
    current = result["returns"]["current"]
    assert current["event_count"] == 0
    assert current["unclassified_records"] == 1
    assert current["unclassified_raw_row_ids"]
