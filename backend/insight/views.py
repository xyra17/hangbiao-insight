from django.contrib.auth import authenticate, get_user_model, login, logout
from django.db import connection, transaction
from django.db.models import Q
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .access import is_admin, products_for, require_role, shops_for
from .models import AuditEntry, Job, Membership, Product, SessionVersion, Shop
from .serializers import (
    AuditSerializer,
    JobCommandSerializer,
    JobSerializer,
    LoginSerializer,
    MappingCommandSerializer,
    ProductCommandSerializer,
    ProductSerializer,
    ShopSerializer,
)
from .services import create_probe, create_product, revise_mapping
from .throttling import LoginThrottle


class SessionView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        responses=inline_serializer(
            name="Session",
            fields={
                "authenticated": serializers.BooleanField(),
                "username": serializers.CharField(allow_null=True),
                "csrf_token": serializers.CharField(),
            },
        )
    )
    def get(self, request):
        return Response(
            {
                "authenticated": request.user.is_authenticated,
                "username": request.user.username if request.user.is_authenticated else None,
                "csrf_token": get_token(request),
            }
        )


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    @extend_schema(
        request=LoginSerializer,
        responses=inline_serializer(
            name="LoginResult",
            fields={"username": serializers.CharField(), "csrf_token": serializers.CharField()},
        ),
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            # Serialize login with credential resets and session revocation.
            get_user_model().objects.select_for_update().filter(
                username=serializer.validated_data["username"]
            ).first()
            user = authenticate(request, **serializer.validated_data)
            if not user:
                raise PermissionDenied("用户名或密码不正确。")
            login(request, user)
            request.session["account_session_version"] = (
                SessionVersion.objects.filter(user=user).values_list("version", flat=True).first() or 0
            )
        return Response({"username": user.username, "csrf_token": get_token(request)})


class LogoutView(APIView):
    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        logout(request)
        return Response(status=204)


class HealthView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses=inline_serializer(name="Health", fields={"status": serializers.CharField()}))
    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception:
            return Response({"status": "unavailable"}, status=503)
        return Response({"status": "ready"})


class ShopViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ShopSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Shop.objects.none()
        return shops_for(self.request.user).select_related("workspace").order_by("name", "id")

    @extend_schema(
        responses=inline_serializer(
            name="Members",
            fields={
                "items": inline_serializer(
                    name="Member",
                    fields={"id": serializers.IntegerField(), "username": serializers.CharField()},
                    many=True,
                )
            },
        )
    )
    @action(detail=True, methods=["get"])
    def members(self, request, pk=None):
        shop = self.get_object()
        require_role(request.user, shop, "data_admin", "operator")
        users = (
            get_user_model().objects.filter(is_active=True, membership__workspace=shop.workspace).distinct()
        )
        users = [u for u in users if shops_for(u).filter(pk=shop.pk).exists()]
        return Response({"items": [{"id": u.pk, "username": u.username} for u in users]})

    @extend_schema(
        responses=inline_serializer(
            name="Capabilities", fields={"roles": serializers.ListField(child=serializers.CharField())}
        )
    )
    @action(detail=True, methods=["get"])
    def capabilities(self, request, pk=None):
        shop = self.get_object()
        roles = (
            ["data_admin", "operator", "quality", "support"]
            if is_admin(request.user, shop)
            else shop.shopgrant_set.get(user=request.user).roles
        )
        return Response({"roles": roles})


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ProductSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Product.objects.none()
        qs = (
            products_for(self.request.user)
            .prefetch_related("mappings__owner")
            .order_by("child_asin", "seller_sku", "id")
        )
        shop = self.request.query_params.get("shop")
        if shop:
            get_object_or_404(shops_for(self.request.user), pk=shop)
            qs = qs.filter(shop_id=shop)
        return qs

    @extend_schema(request=ProductCommandSerializer, responses={201: ProductSerializer})
    def create(self, request):
        serializer = ProductCommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        product = create_product(request.user, shop, data, request.request_id)
        return Response(ProductSerializer(product).data, status=201)

    @extend_schema(request=MappingCommandSerializer, responses=ProductSerializer)
    @action(detail=True, methods=["post"])
    def mappings(self, request, pk=None):
        self.get_object()
        serializer = MappingCommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = revise_mapping(request.user, pk, serializer.validated_data, request.request_id)
        return Response(ProductSerializer(product).data)


class JobViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = JobSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Job.objects.none()
        qs = Job.objects.filter(shop__in=shops_for(self.request.user), actor=self.request.user).order_by(
            "-created_at", "id"
        )
        shop = self.request.query_params.get("shop")
        if shop:
            get_object_or_404(shops_for(self.request.user), pk=shop)
            qs = qs.filter(shop_id=shop)
        from .job_access import visible_jobs

        return visible_jobs(qs, self.request.user)

    @extend_schema(request=None, responses={202: JobSerializer})
    @action(detail=True, methods=["post"], url_path="retry-refresh")
    @transaction.atomic
    def retry_refresh(self, request, pk=None):
        from .api_errors import Conflict
        from .models import DerivedRefresh, Outbox
        from .services import audit
        job = self.get_object()
        job = Job.objects.select_for_update().get(pk=job.pk)
        require_role(request.user, job.shop, "operator", "quality", "support", "data_admin")
        if job.kind != "refresh_derived" or job.status not in {"failed", "cancelled"}:
            raise Conflict("只有已失败或取消的派生刷新任务可重新排队。")
        refresh = get_object_or_404(DerivedRefresh.objects.select_for_update(), job=job)
        job.status, job.error, job.attempts = "queued", "", 0
        job.next_run_at, job.started_at, job.finished_at = None, None, None
        job.save(update_fields=["status", "error", "attempts", "next_run_at", "started_at", "finished_at"])
        refresh.status, refresh.detail = "queued", ""
        refresh.save(update_fields=["status", "detail"])
        Outbox.objects.filter(job=job).update(sent_at=None)
        audit(request.user, job.shop, "artifact.refresh_retried", job.pk, request_id=request.request_id)
        return Response(JobSerializer(job).data, status=202)

    @extend_schema(request=JobCommandSerializer, responses={202: JobSerializer})
    @action(detail=False, methods=["post"])
    def probe(self, request):
        serializer = JobCommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        job = create_probe(request.user, shop, data["idempotency_key"], request.request_id)
        return Response(JobSerializer(job).data, status=202)


class AuditViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AuditSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return AuditEntry.objects.none()
        allowed = [
            s.pk
            for s in shops_for(self.request.user)
            if is_admin(self.request.user, s)
            or (
                "data_admin" in s.shopgrant_set.get(user=self.request.user).roles
                and s.shopgrant_set.get(user=self.request.user).all_products
            )
        ]
        return (
            AuditEntry.objects.filter(
                Q(shop_id__in=allowed)
                | Q(
                    shop__isnull=True,
                    workspace__in=Membership.objects.filter(user=self.request.user, is_admin=True).values(
                        "workspace"
                    ),
                )
            )
            .select_related("actor")
            .order_by("-created_at", "id")
        )
