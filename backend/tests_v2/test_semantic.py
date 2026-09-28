import csv
import io
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from insight.ingestion.service import accept_upload, create_source, parse_batch, publish_batch
from insight.models import BusinessEvent, Notification, SemanticClaim, SemanticRun, StandardReport
from insight.tasks import run_job

pytestmark = pytest.mark.django_db


@pytest.fixture
def review_report(metric_scope, request):
    scope = metric_scope
    users, shops = scope
    rows = [
        {
            "child_asin": "B000TEST01",
            "seller_sku": "S1",
            "fulfillment": "FBA",
            "business_date": date(2026, 9, 1),
            "record_id": "REV-1",
            "title": "Slow charge",
            "text": "Slow charge but compact.",
            "rating": 2,
            "language": "en",
            "collected_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        },
        {
            "child_asin": "B000TEST01",
            "seller_sku": "S1",
            "fulfillment": "FBA",
            "business_date": date(2026, 9, 2),
            "record_id": "REV-2",
            "title": "",
            "text": "It gets hot and stopped working.",
            "rating": 1,
            "language": "en",
            "collected_at": datetime(2026, 9, 2, tzinfo=timezone.utc),
        },
    ]
    if hasattr(request, "param"):
        template = rows[0]
        rows = [
            {
                **template,
                "record_id": item["id"],
                "title": "",
                "text": item["text"],
                "language": item.get("language", "en"),
            }
            for item in request.param
        ]
    source = create_source(
        users["admin"],
        shops[0],
        {
            "name": "评论验收",
            "kind": "reviews",
            "is_primary": True,
            "grain": "product_record",
            "column_mapping": {key: key for key in rows[0]},
            "ignored_columns": [],
        },
        "semantic-test",
    )
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    batch = accept_upload(
        users["admin"],
        source,
        SimpleUploadedFile("reviews.csv", handle.getvalue().encode()),
        {
            "coverage_start": date(2026, 9, 1),
            "coverage_end": date(2026, 9, 2),
            "generated_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
            "declared_complete": True,
            "declared_asins": ["B000TEST01"],
        },
        "semantic-test",
    )
    parse_batch(batch.pk, users["admin"])
    publish_batch(
        users["admin"], batch.pk, allow_partial=False, confirm_contract=True, request_id="semantic-test"
    )
    return users, shops


def test_semantic_run_is_guarded_and_report_is_idempotent(review_report, client):
    users, shops = review_report
    client.force_authenticate(users["admin"])
    response = client.post(
        "/api/v1/semantic-runs/",
        {
            "shop": str(shops[0].pk),
            "child_asin": "B000TEST01",
            "provider": "deterministic",
            "idempotency_key": "semantic-run-1",
        },
        format="json",
    )
    assert response.status_code == 202
    run_job(response.data["id"])
    job = client.get(f"/api/v1/jobs/{response.data['id']}/").data
    assert job["status"] == "completed", job
    run = SemanticRun.objects.get(pk=job["result"]["semantic_run_id"])
    assert run.total_reviews == 2 and run.accepted_claims == 3
    assert SemanticClaim.objects.filter(run=run, prediction_status="accepted").count() == 3
    report = StandardReport.objects.get(pk=job["result"]["report_id"])
    assert report.payload["claims"] and report.payload["citations"][0]["type"] == "semantic_run"
    assert client.get(f"/api/v1/reports/{report.pk}/").status_code == 200
    assert BusinessEvent.objects.filter(family="safety", priority=0).exists()
    assert Notification.objects.filter(kind="safety_immediate").exists()
    assert client.post(
        "/api/v1/semantic-runs/",
        {
            "shop": str(shops[0].pk),
            "child_asin": "B000TEST01",
            "provider": "deterministic",
            "idempotency_key": "semantic-run-1",
        },
        format="json",
    ).data["id"] == str(response.data["id"])


def test_unconfigured_model_is_recorded_as_deferred(review_report, client, monkeypatch):
    users, shops = review_report
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client.force_authenticate(users["admin"])
    response = client.post(
        "/api/v1/semantic-runs/",
        {
            "shop": str(shops[0].pk),
            "child_asin": "B000TEST01",
            "provider": "deepseek",
            "idempotency_key": "semantic-deferred",
        },
        format="json",
    )
    run_job(response.data["id"])
    job = client.get(f"/api/v1/jobs/{response.data['id']}/").data
    assert job["status"] == "completed"
    run = SemanticRun.objects.get(pk=job["result"]["semantic_run_id"])
    assert run.status == "deferred"
    report = StandardReport.objects.get(pk=job["result"]["report_id"])
    assert report.status == "published" and report.payload["status"] == "partial"


def test_lexical_retrieval_has_scope_dates_aliases_and_trace(review_report, client):
    from insight.models import AuditEntry

    users, shops = review_report
    client.force_authenticate(users["a"])
    query = {
        "shop": str(shops[0].pk),
        "child_asin": "B000TEST01",
        "query": "充电慢",
        "start": "2026-09-01",
        "end": "2026-09-02",
    }
    result = client.post("/api/v1/evidence-search/", query, format="json")
    assert result.status_code == 200
    assert [row["record_id"] for row in result.data["items"]] == ["REV-1"]
    assert result.data["items"][0]["raw_row_id"]
    assert AuditEntry.objects.filter(
        action="evidence.retrieved", target_id=result.data["retrieval_id"]
    ).exists()
    empty = client.post("/api/v1/evidence-search/", {**query, "start": "2026-09-02"}, format="json")
    assert empty.data["items"] == []
    client.force_authenticate(users["restricted"])
    assert client.post("/api/v1/evidence-search/", query, format="json").status_code == 403
    client.force_authenticate(users["b"])
    assert client.post("/api/v1/evidence-search/", query, format="json").status_code == 404


RETRIEVAL_CASES = json.loads((Path(__file__).parent / "fixtures/retrieval_cases.json").read_text())


@pytest.mark.parametrize("review_report", [RETRIEVAL_CASES["documents"]], indirect=True)
def test_fixed_bilingual_retrieval_regression(review_report, client):
    from insight.models import Review
    from insight.semantic.indexing import INDEX_VERSION

    users, shops = review_report
    assert Review.objects.exclude(search_index_version=INDEX_VERSION).count() == 0
    assert not Review.objects.filter(search_vector__isnull=True).exists()
    client.force_authenticate(users["a"])
    for case in RETRIEVAL_CASES["queries"]:
        response = client.post(
            "/api/v1/evidence-search/",
            {
                "shop": str(shops[0].pk),
                "child_asin": "B000TEST01",
                "query": case["query"],
                "start": "2026-09-01",
                "end": "2026-09-02",
                "limit": 10,
            },
            format="json",
        )
        assert response.status_code == 200
        retrieved = {item["record_id"] for item in response.data["items"]}
        recall = len(retrieved.intersection(case["relevant"])) / len(case["relevant"])
        assert recall == 1, (case, retrieved)
        if case["query"] == "hot":
            assert "EN-4" not in retrieved
        if case["query"] == "photos":
            assert "CN-1" not in retrieved  # 'hot' must not expand inside 'photos'.


def test_semantic_stops_after_revocation_during_adapter_call(review_report, monkeypatch):
    from rest_framework.exceptions import PermissionDenied

    from insight.models import Membership
    from insight.semantic import service

    users, shops = review_report
    called = []

    class RevokingAdapter(service.DeterministicAdapter):
        def extract(self, text, title=""):
            called.append(text)
            Membership.objects.filter(user=users["admin"]).update(is_admin=False)
            return super().extract(text, title)

    monkeypatch.setattr(service, "adapter_for", lambda provider: RevokingAdapter())
    with pytest.raises(PermissionDenied):
        service.run_semantic(users["admin"], shops[0], "B000TEST01")
    assert len(called) == 1  # No second text leaves the application.
    assert not SemanticRun.objects.exists()
    assert not SemanticClaim.objects.exists()
    assert not StandardReport.objects.exists()


def test_report_rejects_foreign_material_and_revoked_actor(review_report):
    from rest_framework.exceptions import PermissionDenied, ValidationError

    from insight.models import Membership
    from insight.semantic.report import compose_report
    from insight.semantic.service import run_semantic

    users, shops = review_report
    run = run_semantic(users["admin"], shops[0], "B000TEST01")
    with pytest.raises(ValidationError):
        compose_report(users["admin"], shops[1], "B000TEST01", None, run)
    Membership.objects.filter(user=users["admin"]).update(is_admin=False)
    with pytest.raises(PermissionDenied):
        compose_report(users["admin"], shops[0], "B000TEST01", None, run)
    assert not StandardReport.objects.exists()
