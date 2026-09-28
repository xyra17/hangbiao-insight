import json
from datetime import date
from pathlib import Path
from uuid import uuid4
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from insight.models import (
    Workspace,
    Shop,
    Product,
    ProductMapping,
    MetricSnapshot,
    BusinessEvent,
    EventEvidence,
)
assert settings.DEBUG
workspace = Workspace.objects.get(name="航标验收工作区", is_test=True)
actor = get_user_model().objects.get(username="accept_admin")
shop, _ = Shop.objects.get_or_create(workspace=workspace, name="验收 D：事件处理闭环", defaults={"marketplace":"US","timezone":"America/Los_Angeles","currency":"USD"})
asin = "B" + uuid4().hex[:9].upper()
product = Product.objects.create(shop=shop, child_asin=asin, seller_sku=asin, fulfillment="FBA")
ProductMapping.objects.create(product=product,name="合成事件处理样本",effective_from=date(2026,1,1),version=1,reason="隔离浏览器验收")
snapshot = MetricSnapshot.objects.create(shop=shop,child_asin=asin,actor=actor,fingerprint=uuid4().hex,rule_version="workflow-fixture",source_manifest={},payload={},local_date=timezone.now().date())
event = BusinessEvent.objects.create(shop=shop,child_asin=asin,object_key=f"asin:{asin}",family="sales",title="合成流程验收事件",priority=2,last_cutoff=timezone.now().date(),last_seen=timezone.now())
EventEvidence.objects.create(event=event,snapshot=snapshot,metric="ordered_units",detail={"fixture":True})
Path("../.local/workflow-acceptance.json").write_text(json.dumps({"asin":asin,"event":str(event.pk),"shop":str(shop.pk)},ensure_ascii=False))
print("已建立隔离的事件处理验收样本，未改动正式数据。")
