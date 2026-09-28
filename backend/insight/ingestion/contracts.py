import hashlib
import json
from datetime import date, datetime
from datetime import timezone as datetime_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import models
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from insight.models import (
    InventorySnapshot,
    OperationEvent,
    OrderLine,
    Product,
    RatingSnapshot,
    RefundTransaction,
    ReturnLifecycle,
    Review,
    SalesTrafficDaily,
    SupportCase,
)

FACT_MODELS = {
    "sales_traffic": SalesTrafficDaily,
    "orders": OrderLine,
    "refunds": RefundTransaction,
    "returns": ReturnLifecycle,
    "reviews": Review,
    "support": SupportCase,
    "inventory": InventorySnapshot,
    "ratings": RatingSnapshot,
    "operations": OperationEvent,
}
LABELS = {
    "sales_traffic": "销售与流量",
    "orders": "订单与发货",
    "refunds": "退款交易",
    "returns": "退货状态",
    "reviews": "评论",
    "support": "售后工单",
    "inventory": "库存与可售",
    "ratings": "页面星级",
    "operations": "价格促销与运营事件",
}
SYSTEM_FIELDS = {
    "id",
    "created_at",
    "source",
    "batch",
    "raw_row",
    "business_key",
    "payload_hash",
    "revision",
    "product",
    "mapping",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def input_fields(kind):
    return [f for f in FACT_MODELS[kind]._meta.fields if f.name not in SYSTEM_FIELDS]


def field_catalog(kind):
    return [
        {
            "name": f.name,
            "required": not f.null and not f.blank and not f.has_default(),
            "type": f.get_internal_type(),
            "max_length": f.max_length,
        }
        for f in input_fields(kind)
    ]


def validate_contract(kind, grain, mapping, ignored):
    if kind not in FACT_MODELS:
        raise ValidationError("不支持的数据类型。")
    allowed_grains = (
        ["asin_day", "product_day"]
        if kind in {"sales_traffic", "inventory", "ratings"}
        else ["asin_record", "product_record"]
    )
    if grain not in allowed_grains:
        raise ValidationError({"grain": f"该来源粒度须为 {', '.join(allowed_grains)}。"})
    if kind in {"orders", "refunds"} and grain != "product_record":
        raise ValidationError("订单与退款必须提供 SKU 和履约方式以核对订单行。")
    known = {f["name"] for f in field_catalog(kind)}
    required = {f["name"] for f in field_catalog(kind) if f["required"]}
    if grain.startswith("product"):
        required.update({"seller_sku", "fulfillment"})
    if not isinstance(mapping, dict) or set(mapping) - known or required - set(mapping):
        raise ValidationError(
            {"column_mapping": f"字段映射缺少必需字段或含未知字段，必需：{', '.join(sorted(required))}"}
        )
    if any(not isinstance(v, str) or not v.strip() for v in mapping.values()) or len(
        set(mapping.values())
    ) != len(mapping):
        raise ValidationError("每个来源列只能映射到一个字段，且列名不能为空。")
    if set(ignored).intersection(mapping.values()):
        raise ValidationError("已映射字段不能同时忽略。")


def parse_value(field, raw, zone):
    if raw == "":
        if field.null:
            return None
        if field.has_default():
            return field.get_default()
        if field.blank:
            return ""
        raise ValueError(f"{field.name} 不可为空")
    if isinstance(field, models.BooleanField):
        if raw.lower() not in {"true", "false", "1", "0"}:
            raise ValueError(f"{field.name} 须为 true/false")
        return raw.lower() in {"true", "1"}
    if isinstance(field, models.DateTimeField):
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if timezone.is_aware(value):
            return value
        local_zone = ZoneInfo(zone)
        first, second = value.replace(tzinfo=local_zone, fold=0), value.replace(tzinfo=local_zone, fold=1)
        if first.utcoffset() != second.utcoffset() or first.astimezone(datetime_timezone.utc).astimezone(local_zone).replace(tzinfo=None) != value:
            raise ValueError(f"{field.name} 处于夏令时跳变或重复时段，请提供带 UTC 偏移的时间")
        return first
    if isinstance(field, models.DateField):
        return date.fromisoformat(raw)
    if isinstance(field, models.IntegerField):
        if not raw.isdigit():
            raise ValueError(f"{field.name} 须为非负整数")
        value = int(raw)
        if value > 2147483647:
            raise ValueError(f"{field.name} 超出支持范围")
        return value
    if isinstance(field, models.DecimalField):
        value = Decimal(raw)
        if (
            not value.is_finite()
            or value.as_tuple().exponent < -field.decimal_places
            or abs(value) >= Decimal(10) ** (field.max_digits - field.decimal_places)
        ):
            raise ValueError(f"{field.name} 数值或精度不合法")
        return value
    if field.max_length and len(raw) > field.max_length:
        raise ValueError(f"{field.name} 超过长度上限")
    return raw


def load_identity_map(contract, batch):
    identities = {}
    products = Product.objects.filter(
        shop=contract.source.shop, child_asin__in=batch.declared_asins
    ).prefetch_related("mappings")
    for product in products:
        versions = sorted(
            (
                mapping
                for mapping in product.mappings.all()
                if mapping.version <= batch.mapping_manifest.get(str(product.pk), 0)
            ),
            key=lambda mapping: mapping.effective_from,
            reverse=True,
        )
        identities.setdefault(product.child_asin, []).append((product, versions))
    return identities


def normalize_row(contract, batch, raw, identities):
    source = contract.source
    parsed = {
        f.name: parse_value(
            f, (raw.get(contract.column_mapping.get(f.name, "")) or "").strip(), source.shop.timezone
        )
        for f in input_fields(source.kind)
    }
    asin = parsed["child_asin"]
    if len(asin) != 10 or not asin.isascii() or not asin.isalnum() or asin != asin.upper():
        raise ValueError("child_asin 须为 10 位大写字母或数字")
    if asin not in batch.declared_asins:
        raise ValueError("该 ASIN 不在本批次声明的报表范围内")
    day = parsed["business_date"]
    for timestamp_name, date_name in [
        ("shipped_at", "shipped_date"),
        ("occurred_at", "business_date"),
        ("requested_at", "requested_date"),
        ("received_at", "received_date"),
    ]:
        if parsed.get(timestamp_name):
            local_date = parsed[timestamp_name].astimezone(ZoneInfo(source.shop.timezone)).date()
            if parsed.get(date_name) and parsed[date_name] != local_date:
                raise ValueError(f"{timestamp_name} 与 {date_name} 的站点本地日期不一致")
            parsed[date_name] = local_date
    coverage_day = parsed.get("shipped_date") if contract.coverage_basis == "shipped_date" else day
    if source.kind == "returns":
        event_day = parsed.get(f"{contract.return_event}_date")
        if event_day is not None and event_day != day:
            raise ValueError("退货业务日期须与来源契约所选的申请或收到日期一致")
    if coverage_day is None or not batch.coverage_start <= coverage_day <= batch.coverage_end:
        raise ValueError("业务日期不在声明的覆盖窗口内")
    candidates = identities.get(asin, [])
    if contract.grain.startswith("product"):
        matched = next(
            (
                (product, versions)
                for product, versions in candidates
                if product.seller_sku == parsed["seller_sku"] and product.fulfillment == parsed["fulfillment"]
            ),
            None,
        )
        if matched is None:
            raise ValueError("商品映射不存在，请先配置 SKU 与履约方式")
        product, versions = matched
        mapping = next((mapping for mapping in versions if mapping.effective_from <= day), None)
        if mapping is None:
            raise ValueError("业务日期没有有效商品映射")
        parsed.update(product_id=str(product.pk), mapping_id=str(mapping.pk))
    else:
        if parsed["seller_sku"] or parsed["fulfillment"]:
            raise ValueError("ASIN 级来源不能夹带 SKU 或履约字段，请核对原生粒度")
        if not any(mapping.effective_from <= day for _, versions in candidates for mapping in versions):
            raise ValueError("ASIN 在业务日期没有有效映射")
    for name in ("buybox_percent",):
        if parsed.get(name) is not None and not 0 <= parsed[name] <= 100:
            raise ValueError(f"{name} 须在 0 到 100 之间")
    if "rating" in parsed and not 1 <= parsed["rating"] <= 5:
        raise ValueError("rating 须在 1 到 5 之间")
    if parsed.get("currency") and (
        len(parsed["currency"]) != 3 or not parsed["currency"].isalpha() or not parsed["currency"].isupper()
    ):
        raise ValueError("currency 须为三位大写代码")
    if parsed.get("amount") is not None and not parsed.get("currency"):
        raise ValueError("金额必须有币种")
    if source.kind == "orders":
        if parsed["quantity"] == 0 or (
            parsed["shipped_quantity"] is not None and parsed["shipped_quantity"] > parsed["quantity"]
        ):
            raise ValueError("订单数量须大于零且发货数量不能超过订购数量")
        if parsed["shipped_date"] and parsed["shipped_date"] < day:
            raise ValueError("发货日期不能早于订单日期")
    if source.kind in {"inventory", "sales_traffic"}:
        excluded = {
            "child_asin",
            "seller_sku",
            "fulfillment",
            "business_date",
            "product_id",
            "mapping_id",
            "currency",
        }
        if not any(v is not None and v != "" for k, v in parsed.items() if k not in excluded):
            raise ValueError("至少提供一个可用指标；空白不能视为零")
    identity = (
        [parsed.get("record_id")]
        if "record_id" in parsed
        else [str(day), asin, parsed["seller_sku"], parsed["fulfillment"]]
    )
    normalized = json.loads(json.dumps(parsed, default=str))
    return normalized, digest(identity), digest(normalized)
