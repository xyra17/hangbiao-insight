from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied, ValidationError

from .access import is_admin, products_for, require_role
from .api_errors import Conflict
from .models import AuditEntry, Job, Outbox, Product, ProductMapping, Shop


@transaction.atomic
def audit(actor, shop, action, target, before=None, after=None, request_id=""):
    from .execution import check_execution

    check_execution()
    return AuditEntry.objects.create(
        actor=actor,
        workspace=shop.workspace,
        shop=shop,
        action=action,
        target_id=str(target),
        before=before or {},
        after=after or {},
        request_id=request_id,
    )


def validate_owner(owner_id, product):
    if owner_id is None:
        return None
    owner = get_user_model().objects.filter(pk=owner_id, is_active=True).first()
    if owner is None or not products_for(owner).filter(pk=product.pk).exists():
        raise ValidationError({"owner": "负责人必须有该商品的访问权限。"})
    require_role(owner, product.shop, "operator")
    return owner


@transaction.atomic
def create_product(actor, shop, data, request_id):
    shop = Shop.objects.select_for_update().get(pk=shop.pk)
    require_role(actor, shop, "data_admin")
    if not is_admin(actor, shop) and not shop.shopgrant_set.get(user=actor).all_products:
        raise PermissionDenied("新增商品需要该店铺全部商品的数据管理权限。")
    identity = {k: data[k] for k in ("child_asin", "seller_sku", "fulfillment")}
    if Product.objects.filter(shop=shop, **identity).exists():
        raise Conflict("该商品对象已存在，请修改现有映射。")
    product = Product.objects.create(shop=shop, **identity)
    owner = validate_owner(data["owner"], product)
    mapping = ProductMapping.objects.create(
        product=product,
        name=data["name"],
        parent_asin=data["parent_asin"],
        owner=owner,
        effective_from=data["effective_from"],
        version=1,
        reason=data["reason"],
    )
    audit(
        actor,
        shop,
        "product.created",
        product.pk,
        after={"mapping_id": str(mapping.pk), **identity},
        request_id=request_id,
    )
    return product


@transaction.atomic
def revise_mapping(actor, product_id, data, request_id):
    product = get_object_or_404(products_for(actor), pk=product_id)
    Shop.objects.select_for_update().get(pk=product.shop_id)
    product = get_object_or_404(products_for(actor), pk=product_id)
    require_role(actor, product.shop, "data_admin")
    if data.get("expected_version") != product.version:
        raise Conflict()
    previous = product.mappings.order_by("-version").first()
    if data["effective_from"] <= previous.effective_from:
        raise ValidationError({"effective_from": "新映射必须晚于当前映射的生效日期；不能覆盖历史。"})
    owner = validate_owner(data["owner"], product)
    previous.effective_to = data["effective_from"]
    previous.save(update_fields=["effective_to"])
    product.version += 1
    product.save(update_fields=["version"])
    mapping = ProductMapping.objects.create(
        product=product,
        name=data["name"],
        parent_asin=data["parent_asin"],
        owner=owner,
        effective_from=data["effective_from"],
        version=product.version,
        reason=data["reason"],
    )
    audit(
        actor,
        product.shop,
        "product.mapping_revised",
        product.pk,
        before={"mapping_id": str(previous.pk), "version": previous.version},
        after={"mapping_id": str(mapping.pk), "version": product.version},
        request_id=request_id,
    )
    return product


@transaction.atomic
def create_probe(actor, shop, key, request_id):
    require_role(actor, shop, "data_admin")
    job, created = Job.objects.get_or_create(shop=shop, actor=actor, kind="probe", idempotency_key=key)
    if created:
        Outbox.objects.create(job=job)
        audit(actor, shop, "job.created", job.pk, after={"kind": "probe"}, request_id=request_id)
    return job
