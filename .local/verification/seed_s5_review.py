from datetime import timedelta

from django.contrib.auth import get_user_model
from django.conf import settings
from django.utils import timezone

from insight.models import ActionTask, Product, Shop

assert settings.DEBUG
actor = get_user_model().objects.get(username="accept_admin")
shop = Shop.objects.get(name="测试店铺 A", workspace__is_test=True)
product = Product.objects.filter(shop=shop, child_asin="B000TEST01").first()
assert product is not None
task, _ = ActionTask.objects.get_or_create(
    shop=shop,
    child_asin=product.child_asin,
    title="S5 复查展示验收",
    created_by=actor,
    defaults={
        "product": product,
        "status": "completed",
        "effective_at": timezone.now() - timedelta(days=10),
        "review_at": timezone.now(),
        "review_status": "completed",
        "review_result": "观察窗口完整，退货率从 0.12 降至 0.08；保留限制说明。",
        "observed_metrics": ["returns"],
        "review_comparison": {
            "returns": {
                "baseline": "0.12",
                "current": "0.08",
                "delta": "-0.04",
                "unit": "percent",
                "window_start": "2026-09-10",
                "window_end": "2026-09-16",
                "snapshot": "review-fixture",
                "method": "cohort_mature_queue_v1",
                "maturity": {"status": "ready", "immature_orders": 0},
            }
        },
    },
)
if task.product_id != product.pk:
    task.product = product
    task.save(update_fields=["product"])
print("已建立 S5 复查展示验收样本。")
