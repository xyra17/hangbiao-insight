from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied

from .models import Membership, Product, Shop, ShopGrant

ROLES = {"operator", "quality", "support", "data_admin"}


def shops_for(user):
    if not user.is_authenticated or not get_user_model().objects.filter(pk=user.pk, is_active=True).exists():
        return Shop.objects.none()
    memberships = Membership.objects.filter(user=user)
    return (
        Shop.objects.filter(workspace__in=memberships.values("workspace"))
        .filter(
            Q(workspace__in=memberships.filter(is_admin=True).values("workspace"))
            | Q(pk__in=ShopGrant.objects.filter(user=user).exclude(roles=[]).values("shop"))
        )
        .distinct()
    )


def is_admin(user, shop):
    return Membership.objects.filter(user=user, workspace=shop.workspace, is_admin=True).exists()


def require_role(user, shop, *roles):
    if not shops_for(user).filter(pk=shop.pk).exists():
        raise PermissionDenied("无权访问该店铺。")
    if is_admin(user, shop):
        return
    grant = ShopGrant.objects.get(user=user, shop=shop)
    if not set(roles).intersection(grant.roles):
        raise PermissionDenied("当前角色不能执行此操作。")


def products_for(user):
    shops = shops_for(user)
    admin_workspaces = Membership.objects.filter(user=user, is_admin=True).values("workspace")
    grants = ShopGrant.objects.filter(user=user, shop__in=shops)
    return (
        Product.objects.filter(shop__in=shops)
        .filter(
            Q(shop__workspace__in=admin_workspaces)
            | Q(shop__in=grants.filter(all_products=True).values("shop"))
            | Q(pk__in=grants.values("products"))
        )
        .distinct()
    )
