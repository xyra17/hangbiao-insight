from datetime import timedelta

from django.db.models import Exists, OuterRef
from rest_framework.exceptions import PermissionDenied, ValidationError

from insight.access import products_for, shops_for
from insight.ingestion.contracts import FACT_MODELS
from insight.models import CoveragePartition, FactRetraction, Product, Source


def authorize_object(actor, shop, asin, product_id=None):
    if not shops_for(actor).filter(pk=shop.pk).exists():
        raise PermissionDenied("无权访问该店铺。")
    all_objects = Product.objects.filter(shop=shop, child_asin=asin)
    allowed = products_for(actor).filter(shop=shop, child_asin=asin)
    if product_id:
        if not allowed.filter(pk=product_id).exists():
            raise PermissionDenied("无权访问该商品。")
    elif not all_objects.exists() or all_objects.exclude(pk__in=allowed.values("pk")).exists():
        raise PermissionDenied("ASIN 汇总要求具有该 ASIN 下全部商品的访问权限。")


def source_manifest(shop):
    return {
        str(s.pk): {"kind": s.kind, "revision": s.revision}
        for s in Source.objects.filter(shop=shop, is_primary=True).order_by("id")
    }


def source_for(manifest, kind):
    return next(((key, v["revision"]) for key, v in manifest.items() if v["kind"] == kind), (None, 0))


def latest_facts(manifest, kind, asin, product_id=None):
    source_id, revision = source_for(manifest, kind)
    model = FACT_MODELS[kind]
    # Search newer versions by the immutable business key, including corrections that move objects.
    newer = model.objects.filter(
        source_id=source_id,
        business_key=OuterRef("business_key"),
        revision__gt=OuterRef("revision"),
        revision__lte=revision,
    )
    retracted = FactRetraction.objects.filter(
        source_id=source_id,
        business_key=OuterRef("business_key"),
        target_revision=OuterRef("revision"),
        effective_revision__lte=revision,
    )
    qs = model.objects.filter(source_id=source_id, child_asin=asin, revision__lte=revision).filter(
        ~Exists(newer), ~Exists(retracted)
    )
    if product_id:
        qs = qs.filter(product_id=product_id)
    return qs


def coverage_days(manifest, kind, asin, required_basis=None):
    source_id, revision = source_for(manifest, kind)
    partitions = CoveragePartition.objects.filter(
        source_id=source_id, revision__lte=revision, child_asins__contains=[asin]
    ).order_by("revision")
    days = {}
    for partition in partitions.select_related("batch__contract"):
        day = partition.start
        while day <= partition.end:
            # A newer partial revision supersedes the previous completeness declaration.
            days[day] = (
                partition.status
                if required_basis is None or partition.batch.contract.coverage_basis == required_basis
                else "invalid"
            )
            day += timedelta(days=1)
    return days


def complete_range(days, start, end):
    if end < start:
        raise ValidationError("日期范围无效。")
    return all(days.get(start + timedelta(days=i)) == "ready" for i in range((end - start).days + 1))
