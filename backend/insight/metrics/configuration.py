from uuid import uuid4
from zoneinfo import ZoneInfo

from django.core import signing
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from insight.access import shops_for
from insight.api_errors import Conflict
from insight.ingestion.service import require_source_access
from insight.models import Job, Outbox, Product, RuleConfiguration, Shop
from insight.services import audit

from .absolute import calculate_absolute
from .cohort import calculate_cohort
from .daily import calculate_daily
from .facts import authorize_object, source_manifest
from .occurrence import calculate_occurrences
from .parameters import DEFAULTS, INTEGER_FIELDS, PARAMETERS
from .scheduling import enqueue_objects
from .service import RULE_VERSION
from .versions import shop_scope_fingerprint


class RuleSettingsSerializer(serializers.Serializer):
    def get_fields(self):
        return {
            key: (serializers.IntegerField if key in INTEGER_FIELDS else serializers.FloatField)(
                min_value=item[2], max_value=item[3]
            )
            for key, item in PARAMETERS.items()
        }

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) != set(PARAMETERS):
            raise serializers.ValidationError("请提交完整的已定义参数，不接受未知参数。")
        result = super().to_internal_value(data)
        import math

        if any(not math.isfinite(value) for value in result.values()):
            raise serializers.ValidationError("参数必须为有限数值。")
        return result


class RulePreviewCommand(serializers.Serializer):
    shop = serializers.UUIDField()
    child_asin = serializers.RegexField(r"^[A-Z0-9]{10}$")
    product = serializers.UUIDField(required=False, allow_null=True)
    expected_version = serializers.IntegerField(min_value=0)
    values = RuleSettingsSerializer()


class RuleApplyCommand(serializers.Serializer):
    shop = serializers.UUIDField()
    preview_token = serializers.CharField(max_length=30000)
    reason = serializers.CharField(min_length=3, max_length=500)


class RuleResultSerializer(serializers.Serializer):
    version = serializers.IntegerField(required=False)
    values = RuleSettingsSerializer(required=False)
    catalog = serializers.DictField(child=serializers.JSONField(), required=False)
    preview_token = serializers.CharField(required=False)
    before = serializers.ListField(child=serializers.JSONField(), required=False)
    after = serializers.ListField(child=serializers.JSONField(), required=False)
    scope_note = serializers.CharField(required=False)
    history = serializers.ListField(child=serializers.JSONField(), required=False)
    job_id = serializers.UUIDField(required=False)
    impact = serializers.ListField(child=serializers.JSONField(), required=False)


def current_configuration(shop):
    configuration = RuleConfiguration.objects.filter(shop=shop).order_by("-version").first()
    return (configuration.version, configuration.values) if configuration else (0, DEFAULTS)


def preview_signals(manifest, asin, today, shop, product_id, values):
    sections = [
        calculate_daily(manifest, asin, today, product_id, rules=values),
        calculate_absolute(manifest, asin, today, product_id, rules=values),
        calculate_cohort(manifest, asin, today, shop.timezone, product_id, rules=values),
        calculate_occurrences(manifest, asin, today, product_id, rules=values),
    ]
    return [signal for section in sections for signal in section["signals"]]


class RuleConfigurationView(APIView):
    @extend_schema(responses=RuleResultSerializer)
    def get(self, request, shop_id):
        shop = get_object_or_404(shops_for(request.user), pk=shop_id)
        require_source_access(request.user, shop)
        version, values = current_configuration(shop)
        return Response(
            {
                "version": version,
                "values": values,
                "catalog": {
                    key: {"label": item[0], "min": item[2], "max": item[3], "integer": key in INTEGER_FIELDS}
                    for key, item in PARAMETERS.items()
                },
                "history": [
                    {
                        "version": item.version,
                        "reason": item.reason,
                        "created_at": item.created_at.isoformat(),
                        "values": item.values,
                    }
                    for item in RuleConfiguration.objects.filter(shop=shop).order_by("-version")[:20]
                ],
            }
        )


class RulePreviewView(APIView):
    @extend_schema(request=RulePreviewCommand, responses={202: RuleResultSerializer})
    def post(self, request):
        command = RulePreviewCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        require_source_access(request.user, shop)
        authorize_object(request.user, shop, data["child_asin"], data.get("product"))
        version, values = current_configuration(shop)
        if version != data["expected_version"]:
            raise Conflict()
        today = timezone.now().astimezone(ZoneInfo(shop.timezone)).date()
        manifest = source_manifest(shop)
        token_data = {
            "shop": str(shop.pk),
            "actor": request.user.pk,
            "version": version,
            "values": data["values"],
            "manifest": manifest,
            "date": str(today),
            "rule": RULE_VERSION,
            "asin": data["child_asin"],
            "product": str(data["product"]) if data.get("product") else None,
        }
        token_data["scope_fingerprint"] = shop_scope_fingerprint(shop)
        token_data["scope"] = "shop"
        with transaction.atomic():
            require_source_access(request.user, shop)
            job = Job.objects.create(
                shop=shop,
                actor=request.user,
                kind="preview_rules",
                idempotency_key=uuid4().hex,
                payload=token_data,
            )
            Outbox.objects.create(job=job)
            audit(
                request.user,
                shop,
                "rules.preview_requested",
                job.pk,
                after={"version": version, "scope": "shop"},
            )
        return Response({"job_id": str(job.pk)}, status=202)


def assert_preview_current(actor, shop, preview):
    require_source_access(actor, shop)
    version, _ = current_configuration(shop)
    today = timezone.now().astimezone(ZoneInfo(shop.timezone)).date()
    if (
        preview.get("scope") != "shop"
        or preview["shop"] != str(shop.pk)
        or preview["actor"] != actor.pk
        or preview["version"] != version
        or preview["manifest"] != source_manifest(shop)
        or preview["date"] != str(today)
        or preview["rule"] != RULE_VERSION
        or preview.get("scope_fingerprint") != shop_scope_fingerprint(shop)
    ):
        raise Conflict("配置、数据、商品范围或预览身份已变化，请重新预览。")


def build_shop_preview(job, heartbeat):
    from datetime import date

    actor, shop, preview = job.actor, job.shop, job.payload
    assert_preview_current(actor, shop, preview)
    _, old_values = current_configuration(shop)
    products = list(Product.objects.filter(shop=shop).order_by("pk"))
    objects = [(asin, None) for asin in sorted({product.child_asin for product in products})]
    objects += [(p.child_asin, str(p.pk)) for p in products]
    identities = {str(p.pk): p for p in products}
    impact, selected_before, selected_after = [], [], []
    for asin, product_id in objects:
        heartbeat()
        require_source_access(actor, shop)
        args = (preview["manifest"], asin, date.fromisoformat(preview["date"]), shop, product_id)
        before, after = preview_signals(*args, old_values), preview_signals(*args, preview["values"])
        impact.append(
            {
                "child_asin": asin,
                "product": product_id,
                "seller_sku": identities[product_id].seller_sku if product_id else None,
                "fulfillment": identities[product_id].fulfillment if product_id else None,
                "before": before,
                "after": after,
            }
        )
        if asin == preview["asin"] and product_id == preview["product"]:
            selected_before, selected_after = before, after
    with transaction.atomic():
        Shop.objects.select_for_update().get(pk=shop.pk)
        assert_preview_current(actor, shop, preview)
        audit(
            actor,
            shop,
            "rules.preview_completed",
            job.pk,
            after={"version": preview["version"], "object_count": len(impact), "scope": "shop"},
        )
    return {
        "preview_token": signing.dumps(preview, salt="rule-preview", compress=True),
        "before": selected_before,
        "after": selected_after,
        "impact": impact,
        "scope_note": f"已核算全店 {len(impact)} 个 ASIN/SKU 对象。未命中不代表无风险，仍须核对数据覆盖；预览不生成业务事件或通知。保存后全店重扫。",
    }


class RuleApplyView(APIView):
    @extend_schema(request=RuleApplyCommand, responses=RuleResultSerializer)
    @transaction.atomic
    def post(self, request):
        command = RuleApplyCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        shop = get_object_or_404(
            Shop.objects.select_for_update().filter(pk__in=shops_for(request.user)), pk=data["shop"]
        )
        require_source_access(request.user, shop)
        try:
            preview = signing.loads(data["preview_token"], salt="rule-preview", max_age=1800)
        except signing.BadSignature:
            raise Conflict("预览已失效，请重新预览。")
        version, old_values = current_configuration(shop)
        assert_preview_current(request.user, shop, preview)
        settings = RuleSettingsSerializer(data=preview["values"])
        settings.is_valid(raise_exception=True)
        configuration = RuleConfiguration.objects.create(
            shop=shop,
            version=version + 1,
            values=settings.validated_data,
            reason=data["reason"],
            actor=request.user,
        )
        audit(
            request.user,
            shop,
            "rules.configured",
            configuration.pk,
            before={"version": version, "values": old_values},
            after={
                "version": configuration.version,
                "values": configuration.values,
                "reason": configuration.reason,
                "preview_object": {"asin": preview["asin"], "product": preview["product"]},
                "preview_scope": "shop",
                "scope_fingerprint": preview["scope_fingerprint"],
            },
        )
        enqueue_objects(
            request.user,
            shop,
            shop.product_set.values_list("child_asin", flat=True).distinct(),
            f"rules:{configuration.pk}",
        )
        return Response({"version": configuration.version, "values": configuration.values})
