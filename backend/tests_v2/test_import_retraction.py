from datetime import date, datetime, timezone

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from insight.ingestion.service import accept_upload, parse_batch, publish_batch, revoke_published_batch
from insight.metrics.facts import latest_facts, source_manifest
from insight.models import AuditEntry, FactRetraction, SalesTrafficDaily
from insight.services import create_product

pytestmark = pytest.mark.django_db


def test_current_publication_retraction_is_a_revision_and_preserves_history(setup_scope, settings, tmp_path):
    users, shops = setup_scope
    settings.STORAGE_ROOT = tmp_path
    create_product(
        users["admin"], shops[0],
        {
            "child_asin": "B000TEST01", "seller_sku": "S1", "fulfillment": "FBA",
            "name": "测试商品", "parent_asin": "", "owner": None,
            "effective_from": date(2026, 1, 1), "reason": "测试",
        },
        "test",
    )
    from insight.ingestion.service import create_source

    source = create_source(users["admin"], shops[0], {
        "name": "销售", "kind": "sales_traffic", "is_primary": True, "grain": "asin_day",
        "column_mapping": {"child_asin": "asin", "business_date": "date", "ordered_units": "units", "sessions": "sessions"},
        "ignored_columns": [],
    }, "test")
    upload = accept_upload(
        users["admin"], source, SimpleUploadedFile("sales.csv", b"asin,date,units,sessions\nB000TEST01,2026-09-01,10,100\n"),
        {"coverage_start": date(2026, 9, 1), "coverage_end": date(2026, 9, 1), "generated_at": datetime(2026, 9, 2, tzinfo=timezone.utc), "declared_complete": True, "declared_asins": ["B000TEST01"]},
        "test",
    )
    parse_batch(upload.pk, users["admin"])
    publish_batch(users["admin"], upload.pk, allow_partial=False, confirm_contract=True, request_id="test")
    assert latest_facts(source_manifest(shops[0]), "sales_traffic", "B000TEST01").count() == 1
    revoked = revoke_published_batch(users["admin"], upload.pk, reason="来源方确认当前批次错误", request_id="test")
    assert revoked.status == "revoked"
    assert FactRetraction.objects.filter(batch=upload, target_revision=1).count() == 1
    # History and raw facts remain available for audit; the current view hides the target revision.
    assert SalesTrafficDaily.objects.count() == 1
    assert latest_facts(source_manifest(shops[0]), "sales_traffic", "B000TEST01").count() == 0
    assert AuditEntry.objects.filter(action="import.revoked", target_id=str(upload.pk)).exists()
