"""Build a new immutable local index; never runs as a side effect of search."""

import json
import time
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from insight.access import require_role
from insight.metrics.facts import authorize_object, latest_facts, source_manifest
from insight.models import Shop
from insight.semantic.vector_candidates import build_index, index_scope


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        for name in ("actor", "shop", "asin", "kind", "start", "end", "output"):
            parser.add_argument("--" + name, required=True)
        parser.add_argument("--product")

    def handle(self, *args, **options):
        started = time.perf_counter()
        try:
            actor = get_user_model().objects.get(pk=options["actor"])
            shop = Shop.objects.get(pk=options["shop"])
            require_role(actor, shop, "data_admin")
            authorize_object(actor, shop, options["asin"], options["product"])
            if options["kind"] not in {"reviews", "support"}:
                raise ValueError("only feedback kinds supported")
            start, end = date.fromisoformat(options["start"]), date.fromisoformat(options["end"])
            if end < start:
                raise ValueError("invalid index dates")
            output = Path(options["output"])
            if output.exists():
                raise ValueError("unique build report required")
            manifest = source_manifest(shop)
            rows = latest_facts(manifest, options["kind"], options["asin"], options["product"]).filter(business_date__range=(start, end))
            if options["kind"] == "reviews":
                rows = rows.filter(is_deleted=False)
            result = build_index(rows, index_scope(shop, options["asin"], options["product"], options["kind"], manifest), start=start, end=end)
            result.update(build_seconds=round(time.perf_counter() - started, 3), evidence_judged=False,
                          external_transfer=False, quality_accepted=False)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            self.stdout.write(json.dumps({"indexed_rows": result["row_count"], "build_seconds": result["build_seconds"]}))
        except Exception as exc:
            raise CommandError(str(exc)) from exc
