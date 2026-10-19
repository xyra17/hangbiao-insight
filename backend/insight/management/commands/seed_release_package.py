"""Create only isolated identities/products; all facts use the public import API."""
import csv
import json
from datetime import date
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from insight.models import Product, ProductMapping, Shop, ShopGrant, Workspace

from ._acceptance import require_isolated_acceptance_environment


class Command(BaseCommand):
    help = "Seed release package shop/product identities, never business facts."

    def add_arguments(self, parser):
        parser.add_argument("--pack", choices=["04", "05"], required=True)
        parser.add_argument("--pack-dir", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        require_isolated_acceptance_environment()
        target = options["output"].resolve()
        if not target.is_relative_to(Path(settings.BASE_DIR).parent / ".local"):
            raise CommandError("output must be repository-local")
        workspace = Workspace.objects.get(name="航标验收工作区", is_test=True)
        shop, _ = Shop.objects.get_or_create(workspace=workspace, name=f"发布验收 {options['pack']} 全量经营包", marketplace="US",
                                           defaults={"timezone": "America/Los_Angeles", "currency": "USD"})
        owner = get_user_model().objects.get(username="accept_operator_a")
        ShopGrant.objects.get_or_create(user=owner, shop=shop, defaults={"roles": ["operator", "data_admin", "quality", "support"]})
        admin = get_user_model().objects.get(username="accept_admin")
        clock_shop, _ = Shop.objects.get_or_create(
            workspace=workspace,
            name=f"发布验收 {options['pack']} 动作前瞻时间线",
            marketplace="US",
            defaults={"timezone": "America/Los_Angeles", "currency": "USD"},
        )
        ShopGrant.objects.get_or_create(
            user=admin, shop=clock_shop, defaults={"roles": ["operator", "data_admin", "quality", "support"]}
        )
        clock_product, _ = Product.objects.get_or_create(
            shop=clock_shop,
            child_asin="D5X2600005",
            seller_sku=f"D5X2600005-CLOCK-{options['pack']}",
            fulfillment="FBA",
        )
        ProductMapping.objects.get_or_create(
            product=clock_product,
            version=1,
            defaults={
                "name": "[SYNTHETIC] D5X2600005 forward action timeline",
                "effective_from": date(2017, 1, 1),
                "reason": "forward action acceptance timeline",
                "owner": owner,
            },
        )
        complete_clock_shop, _ = Shop.objects.get_or_create(
            workspace=workspace,
            name=f"发布验收 {options['pack']} 完整数据前瞻时间线",
            marketplace="US",
            defaults={"timezone": "America/Los_Angeles", "currency": "USD"},
        )
        ShopGrant.objects.get_or_create(
            user=admin,
            shop=complete_clock_shop,
            defaults={"roles": ["operator", "data_admin", "quality", "support"]},
        )
        complete_clock_product, _ = Product.objects.get_or_create(
            shop=complete_clock_shop,
            child_asin="D5X2600006",
            seller_sku=f"D5X2600006-COMPLETE-CLOCK-{options['pack']}",
            fulfillment="FBA",
        )
        ProductMapping.objects.get_or_create(
            product=complete_clock_product,
            version=1,
            defaults={
                "name": "[SYNTHETIC] D5X2600006 complete forward action timeline",
                "effective_from": date(2017, 1, 1),
                "reason": "complete forward action acceptance timeline",
                "owner": owner,
            },
        )
        identities = set()
        all_asins = set()
        for path in sorted((options["pack_dir"] / "import_ready").rglob("*.csv")):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    asin = row.get("child_asin")
                    if asin:
                        all_asins.add(asin)
                    if asin and row.get("seller_sku"):
                        identities.add((asin, row["seller_sku"], row.get("fulfillment") or "FBA"))
        for asin in all_asins - {item[0] for item in identities}:
            identities.add((asin, f"{asin}-FBA" if options["pack"] == "04" else f"{asin}-SKU1", "FBA"))
        for asin, sku, fulfillment in sorted(identities):
            product, _ = Product.objects.get_or_create(shop=shop, child_asin=asin, seller_sku=sku, fulfillment=fulfillment)
            ProductMapping.objects.get_or_create(product=product, version=1, defaults={"name": f"[SYNTHETIC] {asin} {sku}", "effective_from": date(2017, 1, 1), "reason": "release package replay", "owner": owner})
        result = {"pack": options["pack"], "shop_id": str(shop.pk), "shop_name": shop.name, "timezone": shop.timezone,
                  "products": list(Product.objects.filter(shop=shop).values("id", "child_asin", "seller_sku", "fulfillment")),
                  "forward_action_case": {
                      "shop_id": str(clock_shop.pk), "shop_name": clock_shop.name,
                      "product_id": str(clock_product.pk), "child_asin": clock_product.child_asin,
                      "facts_seeded": 0,
                  },
                  "forward_complete_action_case": {
                      "shop_id": str(complete_clock_shop.pk), "shop_name": complete_clock_shop.name,
                      "product_id": str(complete_clock_product.pk), "child_asin": complete_clock_product.child_asin,
                      "seller_sku": complete_clock_product.seller_sku,
                      "facts_seeded": 0,
                  },
                  "workspace_id": str(workspace.pk), "facts_seeded": 0,
                  "source_nature": "synthetic_or_derived", "admin_username": "accept_admin"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        self.stdout.write(f"Created package {options['pack']} identities: {len(identities)} products; no facts seeded.")
