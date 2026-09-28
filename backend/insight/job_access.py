"""A job receipt must not bypass the current permission of its business result."""
from django.db.models import CharField, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from rest_framework.exceptions import PermissionDenied, ValidationError

from .access import products_for, shops_for
from .ingestion.service import require_source_access
from .investigation.access import visible_investigations
from .metrics.parent import authorize_parent
from .models import ParentSnapshot, Product


def visible_jobs(qs, user):
    qs = qs.annotate(
        scope_product=KeyTextTransform("product_id", "payload"),
        scope_asin=KeyTextTransform("child_asin", "payload"),
        scope_run=KeyTextTransform("run_id", "payload"),
    )
    allowed = products_for(user)
    visible = Q(pk__in=[])
    for shop in shops_for(user):
        local = allowed.filter(shop=shop)
        forbidden = Product.objects.filter(shop=shop).exclude(pk__in=local).values("child_asin")
        product_ids = local.annotate(text_id=Cast("id", CharField())).values("text_id")
        visible |= Q(shop=shop, kind__in=["scan_metrics", "semantic_run", "recompute_metrics", "recompute_metrics_range", "refresh_derived"]) & (
            Q(scope_product__in=product_ids)
            | (Q(scope_product__isnull=True, scope_asin__in=local.values("child_asin")) & ~Q(scope_asin__in=forbidden))
        )
        try:
            require_source_access(user, shop)
        except PermissionDenied:
            pass
        else:
            visible |= Q(shop=shop, kind__in=["probe", "parse_import", "publish_import", "preview_rules"])
        for job in qs.filter(shop=shop, kind="summarize_parent"):
            snapshot = ParentSnapshot.objects.filter(pk=job.result.get("parent_snapshot_id")).first()
            try:
                authorize_parent(user, shop, job.payload["parent_asin"], snapshot.mapping_versions if snapshot else ())
            except (PermissionDenied, ValidationError):
                continue
            visible |= Q(pk=job.pk)
    runs = visible_investigations(user).annotate(text_id=Cast("id", CharField())).values("text_id")
    visible |= Q(kind="investigation_run", scope_run__in=runs)
    return qs.filter(visible)
