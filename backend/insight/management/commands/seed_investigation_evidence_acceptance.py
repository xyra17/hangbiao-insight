import csv
import io
from datetime import date, datetime, time, timedelta, timezone

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError

from insight.ingestion.service import accept_upload, create_source, parse_batch, publish_batch
from insight.models import Product, ProductMapping, Review, Shop, Source, Workspace

from ._acceptance import require_isolated_acceptance_environment


class Command(BaseCommand):
    help = "Publish explicit synthetic original evidence for normal/empty/context browser states."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", type=date.fromisoformat, required=True)

    def handle(self, *args, **options):
        require_isolated_acceptance_environment()
        workspace = Workspace.objects.get(name="航标验收工作区", is_test=True)
        admin = get_user_model().objects.get(username="accept_admin", is_active=True)
        shop, _ = Shop.objects.get_or_create(workspace=workspace, name="验收 F：调查证据边界", marketplace="US",
                                             defaults={"timezone": "America/Los_Angeles", "currency": "USD"})
        product, _ = Product.objects.get_or_create(shop=shop, child_asin="BINV260001", seller_sku="INV-FIXTURE", fulfillment="FBA")
        ProductMapping.objects.get_or_create(product=product, version=1, defaults={"name": "调查证据合成样本",
                                                                               "effective_from": date(2026, 1, 1), "reason": "isolated browser fixture"})
        source_name = f"Synthetic investigation evidence v2 {options['as_of']}"
        existing = Source.objects.filter(shop=shop, name=source_name).first()
        if existing and existing.revision and Review.objects.filter(source=existing, record_id="INV-TITLE").exists():
            self.stdout.write("Synthetic evidence fixture already published; original rows preserved.")
            return
        day = options["as_of"] - timedelta(days=1)
        stamp = datetime.combine(day, time(12), timezone.utc).isoformat()
        texts = [
            ("INV-MIXED", "No overheating occurred on Monday. Overheating occurred on Tuesday. Synthetic acceptance record.", "en"),
            ("INV-PACK", "包装有压痕，商品仍完好。" + "这是专供验收使用的合成原文，不代表真实买家反馈。" * 12, "zh"),
            ("INV-SLOW", "The charger charges slowly, taking several hours. Synthetic acceptance record.", "en"),
            ("INV-NORMAL", "No overheating occurred during normal use. Synthetic acceptance record.", "en"),
        ]
        texts.append(("INV-TITLE", "During supervised use. Synthetic title-only evidence acceptance record.", "en"))
        fields = ["child_asin", "seller_sku", "fulfillment", "business_date", "record_id", "title", "text", "rating", "language", "collected_at"]
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record, text, language in texts:
            writer.writerow(dict(child_asin=product.child_asin, seller_sku=product.seller_sku, fulfillment="FBA", business_date=str(day),
                                 record_id=record, title="Overheating occurred [SYNTHETIC]" if record == "INV-TITLE" else "[SYNTHETIC] 调查证据验收", text=text, rating=3, language=language, collected_at=stamp))
        source = existing or create_source(admin, shop, {"name": source_name, "kind": "reviews", "is_primary": True,
                                                        "grain": "product_record", "coverage_basis": "business_date",
                                                        "column_mapping": {field: field for field in fields}, "ignored_columns": []}, "seed-investigation-evidence")
        batch = accept_upload(admin, source, SimpleUploadedFile("synthetic_investigation_evidence.csv", stream.getvalue().encode()),
                              {"coverage_start": day, "coverage_end": day, "generated_at": datetime.fromisoformat(stamp),
                               "declared_complete": True, "declared_asins": [product.child_asin]}, "seed-investigation-evidence")
        parse_batch(batch.pk, admin)
        batch.refresh_from_db()
        if batch.status != "preview" or batch.error_rows:
            raise CommandError("Synthetic evidence fixture failed import contract validation.")
        publish_batch(admin, batch.pk, allow_partial=False, confirm_contract=True, request_id="seed-investigation-evidence")
        self.stdout.write("Published five synthetic original records in the dedicated evidence acceptance shop.")
