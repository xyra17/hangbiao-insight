"""Read-only persistence audit; expected values come exclusively from CSV oracle."""
import json
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand

from insight.ingestion.contracts import FACT_MODELS
from insight.models import ImportBatch, Job

from ._acceptance import require_isolated_acceptance_environment

FIELDS = {"sales_traffic": ["ordered_units", "order_count", "sessions", "sales_amount"], "orders": ["quantity"],
          "refunds": ["amount"], "returns": ["quantity"], "inventory": ["available", "total"]}


class Command(BaseCommand):
    help = "Read-only persistence and asynchronous job audit for explicitly listed batches."

    def add_arguments(self, parser):
        parser.add_argument("--state", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)

    def handle(self, *args, **options):
        require_isolated_acceptance_environment()
        state = json.loads(options["state"].read_text())
        records = []
        for filename, item in state["files"].items():
            if not item.get("batch_id"):
                continue
            batch = ImportBatch.objects.select_related("raw_file", "contract__source").get(pk=item["batch_id"])
            kind = batch.contract.source.kind
            facts = list(FACT_MODELS[kind].objects.filter(batch=batch))
            totals, missing = {}, {}
            for field in FIELDS.get(kind, []):
                values = [getattr(f, field) for f in facts]
                totals[field] = str(sum((Decimal(str(v)) for v in values if v is not None), Decimal(0)))
                missing[field] = sum(v is None for v in values)
            by_currency = {}
            amount_field = "amount" if kind == "refunds" else "sales_amount" if kind == "sales_traffic" else None
            if amount_field:
                for fact in facts:
                    amount = getattr(fact, amount_field)
                    if amount is not None:
                        by_currency[fact.currency] = by_currency.get(fact.currency, Decimal(0)) + amount
            records.append({"filename": filename, "kind": kind, "batch_id": str(batch.pk), "raw_file_id": str(batch.raw_file_id),
                            "file_sha256": batch.raw_file.sha256, "source_id": str(batch.contract.source_id), "status": batch.status,
                            "input_rows": batch.total_rows, "valid_rows": batch.valid_rows, "rejected_rows": batch.error_rows,
                            "rows": len(facts), "raw_row_links": sum(f.raw_row_id is not None for f in facts),
                            "asins": sorted({f.child_asin for f in facts}), "totals": totals, "missing_numeric_values": missing,
                            "amount_by_currency": {k: str(v) for k, v in sorted(by_currency.items())},
                            "revision": batch.published_revision, "coverage_status": batch.coveragepartition.status,
                            "jobs": list(Job.objects.filter(shop=batch.contract.source.shop, payload__batch_id=str(batch.pk)).values("id", "kind", "status", "attempts", "started_at", "finished_at"))})
        options["output"].write_text(json.dumps({"schema_version": "release-package-db-audit-v1", "read_only": True, "records": records}, ensure_ascii=False, indent=2, default=str))
        self.stdout.write(f"Read-only audit: {len(records)} batches.")
