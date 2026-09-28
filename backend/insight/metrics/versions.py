"""Input identities for operations that span several database transactions."""

from insight.ingestion.contracts import digest
from insight.models import Product, ProductMapping, RuleConfiguration

from .facts import source_manifest


def shop_scope_fingerprint(shop):
    return digest(
        {
            "products": list(
                Product.objects.filter(shop=shop)
                .order_by("pk")
                .values("id", "child_asin", "seller_sku", "fulfillment")
            ),
            "mappings": list(
                ProductMapping.objects.filter(product__shop=shop)
                .order_by("pk")
                .values("id", "product_id", "version", "parent_asin", "effective_from", "effective_to")
            ),
        }
    )


def metric_inputs(shop):
    config = RuleConfiguration.objects.filter(shop=shop).order_by("-version").first()
    return {
        "source_manifest": source_manifest(shop),
        "scope_fingerprint": shop_scope_fingerprint(shop),
        "configuration": str(config.pk) if config else None,
    }
