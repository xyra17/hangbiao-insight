import csv
import io
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from insight.ingestion.service import accept_upload, create_source, parse_batch, publish_batch
from insight.models import Membership, Product, ProductMapping, Shop, ShopGrant, Source

from ._acceptance import require_isolated_acceptance_environment


class Command(BaseCommand):
    help = "在测试工作区导入合成指标样本，不操作正式工作区。"

    def handle(self, *args, **options):
        require_isolated_acceptance_environment()
        user = get_user_model().objects.filter(username="accept_admin", is_active=True).first()
        membership = Membership.objects.filter(user=user, is_admin=True, workspace__is_test=True).first()
        if not membership:
            raise CommandError("请先建立验收测试账号与工作区。")
        shop, _ = Shop.objects.get_or_create(
            workspace=membership.workspace,
            name="验收 C：指标与追溯",
            marketplace="US",
            defaults={"timezone": "America/Los_Angeles", "currency": "USD"},
        )
        product, _ = Product.objects.get_or_create(
            shop=shop, child_asin="B000TEST03", seller_sku="METRIC001", fulfillment="FBA"
        )
        today = timezone.now().astimezone(ZoneInfo(shop.timezone)).date()
        start, end = today - timedelta(days=35), today - timedelta(days=1)
        ProductMapping.objects.get_or_create(
            product=product,
            version=1,
            defaults={
                "name": "合成验收商品：销量与转化下降",
                "effective_from": start - timedelta(days=365),
                "reason": "合成验收数据",
            },
        )
        operator = get_user_model().objects.get(username="accept_operator_a")
        ShopGrant.objects.get_or_create(user=operator, shop=shop, defaults={"roles": ["operator"]})
        rows = [
            {
                "child_asin": "B000TEST03",
                "business_date": start + timedelta(days=i),
                "ordered_units": 20 if i < 28 else 4,
                "order_count": 10 if i < 28 else 2,
                "sessions": 100,
            }
            for i in range(35)
        ]
        source = Source.objects.filter(shop=shop, kind="sales_traffic", is_primary=True).first()
        if source and source.name != "合成验收：销售与流量":
            raise CommandError("此店铺已有其他主来源，不覆盖。")
        source = source or create_source(
            user,
            shop,
            {
                "name": "合成验收：销售与流量",
                "kind": "sales_traffic",
                "is_primary": True,
                "grain": "asin_day",
                "column_mapping": {key: key for key in rows[0]},
                "ignored_columns": [],
            },
            "seed-metrics",
        )
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        batch = accept_upload(
            user,
            source,
            SimpleUploadedFile("synthetic-sales-acceptance.csv", stream.getvalue().encode()),
            {
                "coverage_start": start,
                "coverage_end": end,
                "generated_at": timezone.now(),
                "declared_complete": True,
                "declared_asins": ["B000TEST03"],
            },
            "seed-metrics",
        )
        parse_batch(batch.pk, user)
        publish_batch(user, batch.pk, allow_partial=False, confirm_contract=True, request_id="seed-metrics")
        self.stdout.write(f"合成报表已发布：{shop.name}，{start} 至 {end}。可在事件队列点击扫描。")
