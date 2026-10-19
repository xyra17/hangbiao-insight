"""Controlled frozen metric states, isolated acceptance only; never business data."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from insight.ingestion.contracts import digest
from insight.metrics.facts import source_manifest
from insight.models import DailyScanSchedule, MetricSnapshot, Product, ProductMapping, Shop, Workspace

from ._acceptance import require_isolated_acceptance_environment


class Command(BaseCommand):
    help = "Seed explicit synthetic metric-only states for evidence UI acceptance."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", type=date.fromisoformat, required=True)

    def handle(self, *args, **options):
        require_isolated_acceptance_environment()
        workspace = Workspace.objects.get(name="航标验收工作区", is_test=True)
        admin = get_user_model().objects.get(username="accept_admin")
        shop, _ = Shop.objects.get_or_create(workspace=workspace, name="验收 G：冻结指标证据", marketplace="US",
                                             defaults={"timezone": "America/Los_Angeles", "currency": "USD"})
        DailyScanSchedule.objects.update_or_create(shop=shop, defaults={"actor": admin, "enabled": False})
        today = options["as_of"]
        window = {"start": str(today - timedelta(days=7)), "end": str(today - timedelta(days=1)), "sessions": 200,
                  "ordered_units": 8, "unit_session_percent": "4.0", "complete": True, "currency": "USD", "sales_amount": "20"}
        states = [
            ("BINV260010", {"daily": {"status": "ready", "baseline": {**window, "start": str(today - timedelta(days=35)), "end": str(today - timedelta(days=8))}, "current": window}}),
            ("BINV260011", {"cohort": {"observation_days": 30, "refunds": {"status": "ready", "observation": {"status": "low_sample", "mature_orders": 5}, "current": {"start": window["start"], "end": window["end"], "denominator": 5, "numerator": 1, "rate": "0.2"}}}}),
            ("BINV260012", {"cohort": {"observation_days": 30, "refunds": {"observing_orders": 10, "current": {"denominator": 0, "immature_orders": 10}}}}),
            ("BINV260013", {"daily": {"status": "missing", "reason": "[SYNTHETIC] 缺少完整销售来源，不能补零。"}}),
            ("BINV260014", {"daily": {"status": "ready", "baseline": {**window, "start": str(today - timedelta(days=35)), "end": str(today - timedelta(days=8))}, "current": window}, "cohort": {"refunds": {"status": "missing"}}}),
            ("BINV260015", {"cohort": {"status": "missing", "reason": "[SYNTHETIC] 缺少发货订单覆盖"}, "occurrence": {"refunds": {"status": "ready", "baseline": {"start": str(today - timedelta(days=35)), "end": str(today - timedelta(days=8)), "event_count": 2, "reversal_count": 0, "amount_by_currency": {"USD": "40.00"}}, "current": {"start": window["start"], "end": window["end"], "event_count": 2, "reversal_count": 1, "amount_by_currency": {"USD": "25.00", "EUR": "8.00"}}}}}),
        ]
        for asin, payload in states:
            product, _ = Product.objects.get_or_create(shop=shop, child_asin=asin, seller_sku=f"SYNTHETIC-{asin}", fulfillment="FBA")
            ProductMapping.objects.get_or_create(product=product, version=1, defaults={"name": "受控指标状态，非业务数据", "effective_from": date(2026, 1, 1), "reason": "isolated browser fixture"})
            payload.update(mapping_versions={}, fixture_provenance="[SYNTHETIC] 受控指标状态，非真实报表或业务验收")
            for product_id in (None, product.pk):
                MetricSnapshot.objects.get_or_create(fingerprint=digest({"shop": str(shop.pk), "product": str(product_id), "as_of": str(today), "fixture_version": 5, "payload": payload}),
                    defaults={"shop": shop, "child_asin": asin, "product_id": product_id, "actor": admin, "rule_version": "controlled-metric-evidence-v1", "source_manifest": source_manifest(shop), "payload": payload, "local_date": today})
        self.stdout.write("Seeded six explicit synthetic metric-only states; no original review rows added or changed.")
