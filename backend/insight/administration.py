from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from .access import ROLES
from .api_errors import Conflict
from .models import (
    AuditEntry,
    Membership,
    Product,
    SessionVersion,
    Shop,
    ShopGrant,
    Workspace,
    WorkspaceExecutionPolicy,
    WorkspaceModelBudgetPolicy,
)
from .serializers import ShopSerializer


def require_workspace_admin(user, workspace):
    if (
        not user.is_active
        or not Membership.objects.filter(user=user, workspace=workspace, is_admin=True).exists()
    ):
        raise PermissionDenied("仅工作区管理员可维护店铺与授权。")


def workspace_audit(user, workspace, action_name, target, before, after, request_id):
    AuditEntry.objects.create(
        actor=user,
        workspace=workspace,
        action=action_name,
        target_id=str(target),
        before=before,
        after=after,
        request_id=request_id,
    )


class WorkspaceSerializer(serializers.ModelSerializer):
    is_admin = serializers.SerializerMethodField()

    def get_is_admin(self, obj) -> bool:
        return Membership.objects.filter(
            user=self.context["request"].user, workspace=obj, is_admin=True
        ).exists()

    class Meta:
        model = Workspace
        fields = ["id", "name", "is_test", "timezone", "is_admin"]
        read_only_fields = fields


class WorkspaceMemberSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "user", "username", "is_admin", "version"]
        read_only_fields = fields


class NewMemberCommand(serializers.Serializer):
    username = serializers.RegexField(r"^[A-Za-z0-9_.-]{3,100}$")
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    is_admin = serializers.BooleanField(default=False)

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise ValidationError(exc.messages)
        return value


class MemberChangeCommand(serializers.Serializer):
    member = serializers.UUIDField()
    expected_version = serializers.IntegerField(min_value=1)
    is_admin = serializers.BooleanField()


class SessionRevokeCommand(serializers.Serializer):
    member = serializers.UUIDField()
    expected_version = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=300)


class PasswordResetCommand(SessionRevokeCommand):
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class GrantSerializer(serializers.ModelSerializer):
    roles = serializers.ListField(child=serializers.CharField(), read_only=True)
    products = serializers.PrimaryKeyRelatedField(many=True, read_only=True, pk_field=serializers.UUIDField())
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = ShopGrant
        fields = ["id", "user", "username", "shop", "roles", "all_products", "products", "version"]
        read_only_fields = fields


class GrantCommand(serializers.Serializer):
    user = serializers.IntegerField()
    shop = serializers.UUIDField()
    roles = serializers.ListField(child=serializers.ChoiceField(choices=sorted(ROLES)), allow_empty=True)
    all_products = serializers.BooleanField()
    products = serializers.ListField(child=serializers.UUIDField(), default=list)
    expected_version = serializers.IntegerField(min_value=0)


class NewShopCommand(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    marketplace = serializers.CharField(max_length=20)
    timezone = serializers.CharField(max_length=64)
    currency = serializers.RegexField(r"^[A-Z]{3}$")

    def validate_timezone(self, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValidationError("请填写有效时区，如 America/Los_Angeles。")
        return value


class WorkspaceModelBudgetPolicyCommand(serializers.Serializer):
    enabled = serializers.BooleanField()
    currency = serializers.RegexField(r"^[A-Z]{3}$")
    daily_cost_limit = serializers.DecimalField(max_digits=20, decimal_places=8, min_value=Decimal("0.00000001"))
    monthly_cost_limit = serializers.DecimalField(max_digits=20, decimal_places=8, min_value=Decimal("0.00000001"))
    max_concurrent_external = serializers.IntegerField(min_value=1, max_value=100)
    lease_seconds = serializers.IntegerField(min_value=30, max_value=3600, default=300)

    def validate(self, data):
        if data["monthly_cost_limit"] < data["daily_cost_limit"]:
            raise serializers.ValidationError("月预算不能低于日预算。")
        return data


class WorkspaceModelBudgetPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkspaceModelBudgetPolicy
        fields = ["enabled", "currency", "daily_cost_limit", "monthly_cost_limit", "max_concurrent_external",
                  "lease_seconds", "updated_by", "updated_at"]
        read_only_fields = fields


class WorkspaceExecutionPolicyCommand(serializers.Serializer):
    max_concurrent_investigations = serializers.IntegerField(min_value=1, max_value=100)
    lease_seconds = serializers.IntegerField(min_value=60, max_value=3600, default=900)


class WorkspaceExecutionPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkspaceExecutionPolicy
        fields = ["max_concurrent_investigations", "lease_seconds", "updated_by", "updated_at"]
        read_only_fields = fields


class WorkspaceViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = WorkspaceSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Workspace.objects.none()
        return Workspace.objects.filter(membership__user=self.request.user).distinct().order_by("name", "id")

    @extend_schema(request=WorkspaceModelBudgetPolicyCommand, responses=WorkspaceModelBudgetPolicySerializer)
    @action(detail=True, methods=["get", "put"], url_path="model-budget")
    def model_budget(self, request, pk=None):
        workspace = self.get_object()
        require_workspace_admin(request.user, workspace)
        policy = WorkspaceModelBudgetPolicy.objects.filter(workspace=workspace).first()
        if request.method == "GET":
            if policy is None:
                return Response({"configured": False, "external_dispatch": "blocked_until_admin_configuration"})
            return Response({"configured": True, **WorkspaceModelBudgetPolicySerializer(policy).data})
        command = WorkspaceModelBudgetPolicyCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=workspace.pk)
            policy = WorkspaceModelBudgetPolicy.objects.select_for_update().filter(workspace=workspace).first()
            if policy and policy.currency != data["currency"]:
                from insight.investigation.budget import _committed_cost
                from insight.models import WorkspaceModelReservation

                local_day = timezone.now().astimezone(ZoneInfo(workspace.timezone)).date()
                local_month = local_day.replace(day=1)
                reservations = WorkspaceModelReservation.objects.filter(
                    workspace=workspace,
                    status__in=["reserved", "dispatching", "settled", "settled_overrun", "unknown"],
                )
                daily = _committed_cost(reservations.filter(budget_day=local_day))
                monthly = _committed_cost(reservations.filter(budget_month=local_month))
                if daily > 0 or monthly > 0:
                    raise ValidationError({
                        "currency": "当前日/月预算周期仍有预留、未知或已结金额，暂不能更改币种。"
                    })
            before = WorkspaceModelBudgetPolicySerializer(policy).data if policy else {}
            policy, _ = WorkspaceModelBudgetPolicy.objects.update_or_create(
                workspace=workspace,
                defaults={**data, "updated_by": request.user},
            )
            after = WorkspaceModelBudgetPolicySerializer(policy).data
            workspace_audit(request.user, workspace, "workspace.model_budget_updated", policy.pk,
                            before, after, request.request_id)
        return Response(after)

    @extend_schema(request=WorkspaceExecutionPolicyCommand, responses=WorkspaceExecutionPolicySerializer)
    @action(detail=True, methods=["get", "put"], url_path="execution-policy")
    def execution_policy(self, request, pk=None):
        workspace = self.get_object()
        require_workspace_admin(request.user, workspace)
        policy = WorkspaceExecutionPolicy.objects.filter(workspace=workspace).first()
        if request.method == "GET":
            if policy is None:
                from .investigation.budget import _execution_policy_defaults

                return Response({
                    "configured": False,
                    "effective_default": _execution_policy_defaults(),
                    "free_deterministic_runs": "allowed_with_execution_limit",
                })
            return Response({"configured": True, **WorkspaceExecutionPolicySerializer(policy).data,
                             "free_deterministic_runs": "allowed_with_execution_limit"})
        command = WorkspaceExecutionPolicyCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=workspace.pk)
            policy = WorkspaceExecutionPolicy.objects.filter(workspace=workspace).first()
            before = WorkspaceExecutionPolicySerializer(policy).data if policy else {}
            policy, _ = WorkspaceExecutionPolicy.objects.update_or_create(
                workspace=workspace,
                defaults={**data, "updated_by": request.user},
            )
            after = WorkspaceExecutionPolicySerializer(policy).data
            workspace_audit(request.user, workspace, "workspace.execution_policy_updated", policy.pk,
                            before, after, request.request_id)
        return Response({**after, "free_deterministic_runs": "allowed_with_execution_limit"})

    @extend_schema(responses=WorkspaceMemberSerializer(many=True))
    @action(detail=True, methods=["get"])
    def members(self, request, pk=None):
        workspace = self.get_object()
        require_workspace_admin(request.user, workspace)
        members = (
            Membership.objects.filter(workspace=workspace).select_related("user").order_by("user__username")
        )
        return self.get_paginated_response(
            WorkspaceMemberSerializer(self.paginate_queryset(members), many=True).data
        )

    def change_credentials(self, request, reset_password):
        workspace = self.get_object()
        command = (PasswordResetCommand if reset_password else SessionRevokeCommand)(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=workspace.pk)
            require_workspace_admin(request.user, workspace)
            member = get_object_or_404(Membership, workspace=workspace, pk=data["member"])
            user = get_user_model().objects.select_for_update().get(pk=member.user_id)
            # Credentials apply to the account, not just this workspace.
            administered = Membership.objects.filter(user=request.user, is_admin=True).values("workspace")
            if Membership.objects.filter(user=user).exclude(workspace__in=administered).exists():
                raise PermissionDenied("账号属于其他工作区，需由具备全部相关工作区管理权限的管理员处理。")
            if member.version != data["expected_version"]:
                raise Conflict()
            if reset_password:
                try:
                    validate_password(data["password"], user=user)
                except DjangoValidationError as exc:
                    raise ValidationError({"password": exc.messages})
                user.set_password(data["password"])
                user.save(update_fields=["password"])
            sessions, _ = SessionVersion.objects.get_or_create(user=user)
            sessions.version += 1
            sessions.save(update_fields=["version"])
            before = {"version": member.version}
            member.version += 1
            member.save(update_fields=["version"])
            workspace_audit(
                request.user, workspace,
                "member.password_reset" if reset_password else "member.sessions_revoked",
                member.pk, before,
                {"version": member.version, "reason": data["reason"], "sessions_invalidated": True},
                request.request_id,
            )
        return Response(WorkspaceMemberSerializer(member).data)

    @extend_schema(request=PasswordResetCommand, responses=WorkspaceMemberSerializer)
    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        return self.change_credentials(request, reset_password=True)

    @extend_schema(request=SessionRevokeCommand, responses=WorkspaceMemberSerializer)
    @action(detail=True, methods=["post"], url_path="revoke-sessions")
    def revoke_sessions(self, request, pk=None):
        return self.change_credentials(request, reset_password=False)

    @extend_schema(request=NewMemberCommand, responses={201: WorkspaceMemberSerializer})
    @action(detail=True, methods=["post"], url_path="new-member")
    def new_member(self, request, pk=None):
        workspace = self.get_object()
        command = NewMemberCommand(data=request.data)
        command.is_valid(raise_exception=True)
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=workspace.pk)
            require_workspace_admin(request.user, workspace)
            data = command.validated_data
            if get_user_model().objects.filter(username=data["username"]).exists():
                raise Conflict("用户名不可用，请使用其他用户名。")
            try:
                with transaction.atomic():
                    user = get_user_model().objects.create_user(
                        username=data["username"], password=data["password"]
                    )
            except IntegrityError:
                raise Conflict("用户名不可用，请使用其他用户名。")
            member = Membership.objects.create(workspace=workspace, user=user, is_admin=data["is_admin"])
            workspace_audit(
                request.user,
                workspace,
                "member.created",
                member.pk,
                {},
                {"user_id": user.pk, "is_admin": member.is_admin},
                request.request_id,
            )
        return Response(WorkspaceMemberSerializer(member).data, status=201)

    @extend_schema(request=MemberChangeCommand, responses=WorkspaceMemberSerializer)
    @action(detail=True, methods=["post"], url_path="member-role")
    def member_role(self, request, pk=None):
        workspace = self.get_object()
        command = MemberChangeCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=workspace.pk)
            require_workspace_admin(request.user, workspace)
            member = get_object_or_404(Membership, workspace=workspace, pk=data["member"])
            if member.version != data["expected_version"]:
                raise Conflict()
            if (
                member.is_admin
                and not data["is_admin"]
                and not Membership.objects.filter(workspace=workspace, is_admin=True, user__is_active=True)
                .exclude(pk=member.pk)
                .exists()
            ):
                raise ValidationError("至少保留一名有效工作区管理员。")
            before = {"is_admin": member.is_admin, "version": member.version}
            member.is_admin, member.version = data["is_admin"], member.version + 1
            member.save(update_fields=["is_admin", "version"])
            workspace_audit(
                request.user,
                workspace,
                "member.role_changed",
                member.pk,
                before,
                {"is_admin": member.is_admin, "version": member.version},
                request.request_id,
            )
        return Response(WorkspaceMemberSerializer(member).data)

    @extend_schema(responses=GrantSerializer(many=True))
    @action(detail=True, methods=["get"])
    def grants(self, request, pk=None):
        workspace = self.get_object()
        require_workspace_admin(request.user, workspace)
        qs = (
            ShopGrant.objects.filter(shop__workspace=workspace)
            .select_related("user")
            .prefetch_related("products")
            .order_by("user__username", "id")
        )
        return self.get_paginated_response(GrantSerializer(self.paginate_queryset(qs), many=True).data)

    @extend_schema(request=GrantCommand, responses=GrantSerializer)
    @action(detail=True, methods=["post"], url_path="set-grant")
    def set_grant(self, request, pk=None):
        workspace = self.get_object()
        command = GrantCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=workspace.pk)
            require_workspace_admin(request.user, workspace)
            member = get_object_or_404(Membership, workspace=workspace, user_id=data["user"])
            shop = get_object_or_404(Shop, workspace=workspace, pk=data["shop"])
            products = list(Product.objects.filter(shop=shop, pk__in=data["products"]))
            if len(products) != len(set(data["products"])):
                raise ValidationError("商品范围包含无效或其他店铺对象。")
            grant = ShopGrant.objects.filter(user=member.user, shop=shop).first()
            if data["expected_version"] != (grant.version if grant else 0):
                raise Conflict()
            before = GrantSerializer(grant).data if grant else {}
            if grant is None:
                grant = ShopGrant(user=member.user, shop=shop)
            else:
                grant.version += 1
            grant.roles = sorted(set(data["roles"]))
            grant.all_products = data["all_products"] if grant.roles else False
            grant.save()
            grant.products.set(products if grant.roles and not grant.all_products else [])
            # Only JSON scalars are persisted; the password never enters an audit or job payload.
            after = {
                "user": member.user_id,
                "shop": str(shop.pk),
                "roles": grant.roles,
                "all_products": grant.all_products,
                "products": [str(p.pk) for p in products] if grant.roles and not grant.all_products else [],
                "version": grant.version,
            }
            workspace_audit(
                request.user,
                workspace,
                "grant.updated",
                grant.pk,
                {
                    **{key: before[key] for key in ["roles", "all_products", "version"] if key in before},
                    "products": [str(value) for value in before.get("products", [])],
                },
                after,
                request.request_id,
            )
        return Response(GrantSerializer(grant).data)

    @extend_schema(request=NewShopCommand, responses={201: ShopSerializer})
    @action(detail=True, methods=["post"], url_path="new-shop")
    def new_shop(self, request, pk=None):
        workspace = self.get_object()
        command = NewShopCommand(data=request.data)
        command.is_valid(raise_exception=True)
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=workspace.pk)
            require_workspace_admin(request.user, workspace)
            data = command.validated_data
            if Shop.objects.filter(
                workspace=workspace, name=data["name"], marketplace=data["marketplace"]
            ).exists():
                raise Conflict("此店铺与站点已登记。")
            shop = Shop.objects.create(workspace=workspace, **data)
            workspace_audit(request.user, workspace, "shop.created", shop.pk, {}, data, request.request_id)
        return Response(ShopSerializer(shop).data, status=201)
