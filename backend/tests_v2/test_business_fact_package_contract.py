import csv
import io
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from insight.ingestion.contracts import FACT_MODELS, input_fields, parse_value, validate_contract
from insight.ingestion.service import (
    accept_upload,
    create_source,
    parse_batch,
    publish_batch,
    revoke_published_batch,
)
from insight.metrics.facts import latest_facts, source_manifest
from insight.models import CoveragePartition, Product, ProductMapping
from insight.semantic.retrieval import retrieve

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
IMPORT_ROOT = REPOSITORY_ROOT / "dataset" / "data" / "04_经营事实增强" / "import_ready"


class BusinessFactPackageContractTests(SimpleTestCase):
    def test_all_import_files_match_django_contracts_and_parse(self):
        sidecars = sorted(IMPORT_ROOT.rglob("*.csv.meta.json"))
        self.assertEqual(len(sidecars), 16)

        for sidecar_path in sidecars:
            with self.subTest(sidecar=sidecar_path.name):
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
                payload = sidecar["source_payload"]
                kind = payload["kind"]
                csv_path = Path(str(sidecar_path)[: -len(".meta.json")])
                model = FACT_MODELS[kind]
                mapping = payload["column_mapping"]
                ignored = payload["ignored_columns"]
                known_fields = {field.name for field in input_fields(kind)}

                validate_contract(kind, payload["grain"], mapping, ignored)
                self.assertTrue(set(mapping) <= known_fields)
                self.assertFalse(set(mapping.values()) & set(ignored))

                with csv_path.open(encoding="utf-8", newline="") as source:
                    reader = csv.DictReader(source)
                    headers = set(reader.fieldnames or [])
                    self.assertEqual(headers, set(mapping.values()) | set(ignored))
                    for line, row in enumerate(reader, start=2):
                        for field_name in mapping:
                            parse_value(
                                model._meta.get_field(field_name),
                                (row.get(mapping[field_name]) or "").strip(),
                                "America/Los_Angeles",
                            )
                        if kind == "support":
                            self.assertEqual(row["is_synthetic"], "true")
                            self.assertTrue(row["synthetic_from_review"])
                            self.assertTrue(row["text"].startswith("[SYNTHETIC SUPPORT CASE]"))
                        self.assertGreaterEqual(line, 2)


def make_test_products(shop):
    products = {}
    for asin in ("B07H2V5YLH", "B07P9S6R9Y", "B0BYJ8ZVWH"):
        product = Product.objects.create(
            shop=shop,
            child_asin=asin,
            seller_sku=f"{asin}-FBA",
            fulfillment="FBA",
        )
        ProductMapping.objects.create(
            product=product,
            name=f"Synthetic fixture {asin}",
            effective_from=date(2017, 1, 1),
            version=1,
            reason="business fact package contract test",
        )
        products[asin] = product
    return products


def package_upload(sidecar_path, user, shop, source=None):
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    source_payload = dict(sidecar["source_payload"])
    source_payload.pop("shop", None)
    source = source or create_source(user, shop, source_payload, "business-fact-package-test")
    csv_path = Path(str(sidecar_path)[: -len(".meta.json")])
    with csv_path.open(encoding="utf-8", newline="") as source_file:
        reader = csv.DictReader(source_file)
        headers = list(reader.fieldnames or [])
        first_row = next(reader)
    # One representative row keeps the API workflow small; the full file is
    # checked separately by the parser contract test above.
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader()
    writer.writerow(first_row)
    upload_info = sidecar["upload_payload"]
    business_day = date.fromisoformat(first_row["business_date"])
    upload_data = {
        "coverage_start": business_day,
        "coverage_end": business_day,
        "generated_at": datetime.fromisoformat(upload_info["generated_at"].replace("Z", "+00:00")),
        "declared_complete": False,
        "declared_asins": [first_row["child_asin"]],
    }
    batch = accept_upload(
        user,
        source,
        SimpleUploadedFile(csv_path.name, stream.getvalue().encode("utf-8"), content_type="text/csv"),
        upload_data,
        "business-fact-package-test",
    )
    parse_batch(batch.pk, user)
    batch.refresh_from_db()
    assert batch.status == "preview", list(batch.rows.values_list("error", flat=True))
    assert batch.error_rows == 0
    publish_batch(user, batch.pk, allow_partial=False, confirm_contract=True, request_id="business-fact-package-test")
    return source, batch, first_row


def publish_boundary_file(file_path, kind, user, shop, coverage_day, generated_at, declared_asins):
    import_manifest = json.loads((IMPORT_ROOT.parent / "manifest.json").read_text(encoding="utf-8"))
    info = next(value for value in import_manifest["files"].values() if value["fact_kind"] == kind)
    grain = info["grain"]
    with file_path.open(encoding="utf-8", newline="") as source_file:
        reader = csv.DictReader(source_file)
        headers = list(reader.fieldnames or [])
        rows = list(reader)
    source = create_source(
        user,
        shop,
        {
            "name": f"package boundary: {file_path.name}",
            "kind": kind,
            "is_primary": False,
            "grain": grain,
            "column_mapping": {field: field for field in info["contract_fields"]},
            "ignored_columns": [field for field in headers if field not in info["contract_fields"]],
            **({"return_event": "received"} if kind == "returns" else {}),
        },
        "business-fact-boundary-test",
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    batch = accept_upload(
        user,
        source,
        SimpleUploadedFile(file_path.name, stream.getvalue().encode("utf-8"), content_type="text/csv"),
        {
            "coverage_start": coverage_day,
            "coverage_end": coverage_day,
            "generated_at": generated_at,
            "declared_complete": False,
            "declared_asins": declared_asins,
        },
        "business-fact-boundary-test",
    )
    parse_batch(batch.pk, user)
    batch.refresh_from_db()
    return source, batch


@pytest.mark.django_db
def test_package_rows_publish_for_each_generated_fact_domain(setup_scope, settings, tmp_path):
    settings.STORAGE_ROOT = tmp_path
    users, shops = setup_scope
    shop = shops[0]
    make_test_products(shop)
    for filename in (
        "sales_traffic_daily_v1.csv",
        "ratings_daily_v1.csv",
        "inventory_daily_v1.csv",
        "operations_v1.csv",
        "orders_v1.csv",
        "refunds_v1.csv",
        "returns_v1.csv",
        "support_cases_v1.csv",
    ):
        sidecar_path = IMPORT_ROOT / f"{filename}.meta.json"
        source, batch, _ = package_upload(sidecar_path, users["admin"], shop)
        fact = FACT_MODELS[source.kind].objects.get(batch=batch)
        assert fact.raw_row.raw["is_real"] == "false"
        assert fact.raw_row.raw["is_synthetic"] == "true" or fact.raw_row.raw["is_derived"] == "true"
        if source.kind == "support":
            assert fact.raw_row.raw["synthetic_from_review"]
            assert fact.text.startswith("[SYNTHETIC SUPPORT CASE]")
            evidence = retrieve(
                users["admin"],
                shop,
                fact.child_asin,
                query="synthetic support case",
                start=fact.business_date,
                end=fact.business_date,
                kind="support",
            )
            assert evidence["items"] and evidence["items"][0]["record_id"] == fact.record_id


@pytest.mark.django_db
def test_package_revision_retraction_and_return_lifecycle_fixtures(setup_scope, settings, tmp_path):
    settings.STORAGE_ROOT = tmp_path
    users, shops = setup_scope
    shop = shops[0]
    make_test_products(shop)
    revision_dir = IMPORT_ROOT / "lifecycle_revisions"

    sales_source = None
    for filename in ("sales_traffic_revision_v1.csv", "sales_traffic_revision_v2.csv"):
        sales_source, _, _ = package_upload(revision_dir / f"{filename}.meta.json", users["admin"], shop, sales_source)
    sales_rows = list(latest_facts(source_manifest(shop), "sales_traffic", "B07H2V5YLH"))
    assert len(sales_rows) == 1 and sales_rows[0].revision == 2 and sales_rows[0].sessions == 900

    returns_source = None
    for filename in ("returns_requested_v1.csv", "returns_received_v2.csv", "returns_cancelled_v3.csv"):
        returns_source, _, _ = package_upload(revision_dir / f"{filename}.meta.json", users["admin"], shop, returns_source)
    return_rows = list(latest_facts(source_manifest(shop), "returns", "B07H2V5YLH"))
    assert len(return_rows) == 1 and return_rows[0].revision == 3 and return_rows[0].status == "cancelled"

    _, batch, _ = package_upload(
        revision_dir / "order_retraction_candidate.csv.meta.json", users["admin"], shop
    )
    revoke_published_batch(users["admin"], batch.pk, reason="package retraction fixture", request_id="package-revoke")
    assert FACT_MODELS["orders"].objects.filter(batch=batch).count() == 1
    assert not latest_facts(source_manifest(shop), "orders", "B07H2V5YLH").filter(batch=batch).exists()


@pytest.mark.django_db
def test_package_invalid_duplicate_and_explicit_empty_report_boundaries(setup_scope, settings, tmp_path):
    settings.STORAGE_ROOT = tmp_path
    users, shops = setup_scope
    for shop in shops:
        make_test_products(shop)
    invalid_dir = IMPORT_ROOT.parent / "boundary_cases"
    invalid_specs = [
        ("orders_invalid_v1.csv", "orders", "child_asin"),
        ("orders_duplicate_v1.csv", "orders", "重复"),
        ("refunds_invalid_v1.csv", "refunds", "currency"),
        ("returns_invalid_v1.csv", "returns", "收到日期"),
    ]
    for filename, kind, expected_error in invalid_specs:
        _, batch = publish_boundary_file(
            invalid_dir / filename,
            kind,
            users["admin"],
            shops[0],
            date(2023, 1, 1),
            datetime(2026, 10, 1, tzinfo=timezone.utc),
            ["B07P9S6R9Y"],
        )
        assert batch.status == "invalid"
        assert batch.error_rows > 0
        assert any(expected_error in error.lower() for error in batch.rows.values_list("error", flat=True))

    empty_sidecar_path = IMPORT_ROOT / "refunds_empty_report_v1.csv.meta.json"
    empty_meta = json.loads(empty_sidecar_path.read_text(encoding="utf-8"))
    payload = dict(empty_meta["source_payload"])
    payload.pop("shop", None)
    empty_source = create_source(users["admin"], shops[1], payload, "business-fact-empty-test")
    empty_csv = IMPORT_ROOT / "refunds_empty_report_v1.csv"
    empty_upload = empty_meta["upload_payload"]
    empty_batch = accept_upload(
        users["admin"],
        empty_source,
        SimpleUploadedFile(empty_csv.name, empty_csv.read_bytes(), content_type="text/csv"),
        {
            "coverage_start": date.fromisoformat(empty_upload["coverage_start"]),
            "coverage_end": date.fromisoformat(empty_upload["coverage_end"]),
            "generated_at": datetime.fromisoformat(empty_upload["generated_at"].replace("Z", "+00:00")),
            "declared_complete": True,
            "declared_asins": empty_upload["declared_asins"],
        },
        "business-fact-empty-test",
    )
    parse_batch(empty_batch.pk, users["admin"])
    empty_batch.refresh_from_db()
    assert empty_batch.status == "preview" and empty_batch.total_rows == 0
    publish_batch(users["admin"], empty_batch.pk, allow_partial=False, confirm_contract=True, request_id="business-fact-empty-test")
    assert CoveragePartition.objects.get(batch=empty_batch).status == "ready"
