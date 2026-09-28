import csv
import io
from datetime import date, datetime, timezone

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models
from rest_framework.exceptions import ValidationError

from insight.ingestion.contracts import FACT_MODELS, parse_value
from insight.ingestion.service import accept_upload, create_source, parse_batch, publish_batch, store_file
from insight.metrics.facts import latest_facts, source_manifest
from insight.models import RawFile
from insight.semantic.service import run_semantic

pytestmark = pytest.mark.django_db

SAMPLES = {
    "sales_traffic":{"ordered_units":10,"sessions":100},
    "orders":{"record_id":"O1-L1","order_id":"O1","quantity":2,"status":"shipped","shipped_date":"2026-09-01"},
    "refunds":{"record_id":"F1","order_line_id":"O1-L1","transaction_chain_id":"C1","amount":"12.00","currency":"USD"},
    "returns":{"record_id":"T1","order_line_id":"O1-L1","quantity":1,"status":"received","received_date":"2026-09-01"},
    "reviews":{"record_id":"R1","title":"真实反馈","text":"It gets hot.","rating":1,"language":"en","collected_at":"2026-09-02T00:00:00Z","is_deleted":False},
    "support":{"record_id":"S1","text":"外箱破损","status":"open"},
    "inventory":{"available":0,"listing_status":"active"},
    "ratings":{"rating":"4.20","rating_count":10},
    "operations":{"record_id":"P1","event_type":"price","description":"调价记录","price":"12.00","currency":"USD"},
}


def import_rows(actor, source, rows):
    stream=io.StringIO(newline="")
    writer=csv.DictWriter(stream,fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    batch=accept_upload(actor,source,SimpleUploadedFile("sample.csv",stream.getvalue().encode()),{
        "coverage_start":date(2026,9,1),"coverage_end":date(2026,9,1),
        "generated_at":datetime(2026,9,3,tzinfo=timezone.utc),"declared_complete":True,"declared_asins":["B000TEST01"]},"boundary-test")
    parse_batch(batch.pk,actor)
    batch.refresh_from_db()
    assert batch.error_rows == 0, list(batch.rows.values_list("error",flat=True))
    publish_batch(actor,batch.pk,allow_partial=False,confirm_contract=True,request_id="boundary-test")
    return batch


def source_sample(metric_scope, kind):
    users,shops=metric_scope
    row={"child_asin":"B000TEST01","business_date":"2026-09-01", **SAMPLES[kind]}
    grain="asin_day" if kind in {"sales_traffic","inventory","ratings"} else "asin_record"
    if kind in {"orders","refunds"}:
        row.update(seller_sku="S1",fulfillment="FBA")
        grain="product_record"
    source=create_source(users["admin"],shops[0],{"name":kind,"kind":kind,"is_primary":True,"grain":grain,
        "column_mapping":{k:k for k in row},"ignored_columns":[]},"boundary-test")
    return users,shops,source,row


@pytest.mark.parametrize("kind",SAMPLES)
def test_nine_synthetic_contracts_preserve_native_fields_and_raw_trace(metric_scope, kind):
    users,shops,source,row=source_sample(metric_scope,kind)
    batch=import_rows(users["admin"],source,[row])
    fact=FACT_MODELS[kind].objects.get(batch=batch)
    assert fact.raw_row.line == 2 and fact.raw_row.raw["child_asin"] == "B000TEST01"
    assert fact.revision == 1
    assert fact.product_id is not None if kind in {"orders","refunds"} else fact.product_id is None
    if kind == "inventory":
        assert fact.available == 0 and fact.total is None


def test_review_delete_restore_and_omission_preserve_frozen_versions(metric_scope):
    users,shops,source,row=source_sample(metric_scope,"reviews")
    import_rows(users["admin"],source,[row])
    first=source_manifest(shops[0])
    import_rows(users["admin"],source,[{**row,"record_id":"R2","text":"Works well."}])
    assert latest_facts(source_manifest(shops[0]),"reviews","B000TEST01").count() == 2
    import_rows(users["admin"],source,[{**row,"is_deleted":True}])
    current=source_manifest(shops[0])
    assert latest_facts(current,"reviews","B000TEST01").get(record_id="R1").is_deleted
    assert not latest_facts(first,"reviews","B000TEST01").get().is_deleted
    run=run_semantic(users["admin"],shops[0],"B000TEST01")
    assert run.total_reviews == 1 and not run.claims.exists()
    import_rows(users["admin"],source,[{**row,"text":"It gets hot again.","is_deleted":False}])
    assert run_semantic(users["admin"],shops[0],"B000TEST01").total_reviews == 2
    assert FACT_MODELS["reviews"].objects.count() == 4


def test_naive_dst_gap_and_overlap_require_explicit_offset():
    field=models.DateTimeField()
    field.name="occurred_at"
    for value in ["2026-03-08T02:30:00","2026-11-01T01:30:00"]:
        with pytest.raises(ValueError,match="UTC"):
            parse_value(field,value,"America/Los_Angeles")
    assert parse_value(field,"2026-11-01T01:30:00-08:00","America/Los_Angeles").utcoffset().total_seconds() == -28800


def test_oversized_stream_is_rejected_and_temporary_file_removed(settings,tmp_path):
    settings.STORAGE_ROOT=tmp_path
    class LargeStream:
        def chunks(self):
            for _ in range(51):
                yield b"x"*(1024*1024)
    with pytest.raises(ValidationError):
        store_file(LargeStream())
    assert not RawFile.objects.exists() and list(tmp_path.iterdir()) == []


def test_revision_queues_existing_historical_windows_without_overwriting(metric_scope):
    from insight.metrics.service import scan_object
    from insight.models import Job, MetricSnapshot
    from insight.tasks import run_job
    users, shops, source, row = source_sample(metric_scope, "sales_traffic")
    import_rows(users["admin"], source, [row])
    old = scan_object(users["admin"], shops[0], row["child_asin"], today=date(2026,9,2), record_events=False)
    old_payload = old.payload
    revised = import_rows(users["admin"], source, [{**row,"ordered_units":20}])
    jobs = list(Job.objects.filter(kind="recompute_metrics"))
    assert len(jobs) == 1 and jobs[0].payload["as_of"] == "2026-09-02"
    run_job(str(jobs[0].pk))
    jobs[0].refresh_from_db()
    assert jobs[0].status == "completed"
    fresh = MetricSnapshot.objects.get(pk=jobs[0].result["snapshot_id"])
    old.refresh_from_db()
    assert old.payload == old_payload and fresh.pk != old.pk
    assert fresh.source_manifest[str(source.pk)]["revision"] == 2
    assert fresh.payload["mode"] == "historical"
    publish_batch(users["admin"], revised.pk, allow_partial=False, confirm_contract=True, request_id="repeat")
    assert Job.objects.filter(kind="recompute_metrics").count() == 1


@pytest.mark.parametrize("kind", SAMPLES)
def test_all_source_domains_hide_ignored_private_columns_from_operator(metric_scope, kind, client):
    from insight.models import SourceContract
    users, _, source, row = source_sample(metric_scope, kind)
    contract = SourceContract.objects.get(source=source)
    contract.ignored_columns = ["buyer_email", "buyer_address"]
    contract.save()
    batch = import_rows(users["admin"], source, [{**row,"buyer_email":"private@example.test","buyer_address":"private address"}])
    raw = batch.rows.get()
    client.force_authenticate(users["a"])
    response = client.get(f"/api/v1/fact-rows/{raw.pk}/")
    assert response.status_code == 200
    assert "private@example.test" not in str(response.data) and "private address" not in str(response.data)
    client.force_authenticate(users["b"])
    assert client.get(f"/api/v1/fact-rows/{raw.pk}/").status_code == 404
