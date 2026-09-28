from django.db import transaction
from django.db.models import BooleanField, Case, F, Q, Value, When
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from insight.access import products_for, require_role, shops_for
from insight.ingestion.api import RowSerializer
from insight.ingestion.service import require_source_access
from insight.models import (
    ActionTask,
    BusinessEvent,
    EventEvidence,
    Job,
    MetricSnapshot,
    Notification,
    ObservationLead,
    Outbox,
    Product,
)
from insight.serializers import JobSerializer

from .event_commands import EventCommand, change_event
from .facts import authorize_object


def can_read_raw_source(user, shop):
    from rest_framework.exceptions import PermissionDenied

    try:
        require_source_access(user, shop)
    except PermissionDenied:
        return False
    return True


class FactRowView(APIView):
    @extend_schema(responses=RowSerializer)
    def get(self, request, row_id):
        from django.http import Http404
        from rest_framework.exceptions import PermissionDenied

        from insight.ingestion.contracts import FACT_MODELS
        from insight.models import StagedRow

        row = get_object_or_404(
            StagedRow.objects.select_related("batch__contract__source__shop"),
            pk=row_id,
            batch__status="published",
        )
        source = row.batch.contract.source
        fact = get_object_or_404(FACT_MODELS[source.kind], raw_row=row)
        try:
            authorize_object(request.user, source.shop, fact.child_asin, fact.product_id)
        except PermissionDenied:
            raise Http404
        data = RowSerializer(row).data
        if not can_read_raw_source(request.user, source.shop):
            mapping = row.batch.contract.column_mapping
            data["raw"] = {
                column: value for column, value in data["raw"].items() if column in mapping.values()
            }
            data["normalized"] = dict(data["normalized"])
            for key in ["order_id", "order_line_id", "original_line_id"] + (
                ["record_id"] if source.kind == "orders" else []
            ):
                if value := data["normalized"].get(key):
                    data["normalized"][key] = "••••" + str(value)[-4:]
                if column := mapping.get(key):
                    if value := data["raw"].get(column):
                        data["raw"][column] = "••••" + str(value)[-4:]
        return Response(data)


class ScanCommand(serializers.Serializer):
    shop = serializers.UUIDField()
    child_asin = serializers.RegexField(r"^[A-Z0-9]{10}$")
    product = serializers.UUIDField(required=False, allow_null=True)
    idempotency_key = serializers.CharField(max_length=150)


class RecomputeCommand(ScanCommand):
    as_of = serializers.DateField()


class RecomputeRangeCommand(ScanCommand):
    from_date = serializers.DateField()
    to_date = serializers.DateField()

    def validate(self, attrs):
        if attrs["to_date"] < attrs["from_date"]:
            raise serializers.ValidationError("历史回补结束日期不能早于开始日期。")
        if (attrs["to_date"] - attrs["from_date"]).days >= 366:
            raise serializers.ValidationError("单次历史回补最多覆盖 366 天。")
        return attrs


class SnapshotSerializer(serializers.ModelSerializer):
    payload = serializers.DictField(child=serializers.JSONField(), read_only=True)

    def to_representation(self, instance):
        from copy import deepcopy

        data = super().to_representation(instance)
        if not can_read_raw_source(self.context["request"].user, instance.shop):
            data["payload"] = deepcopy(data["payload"])
            for kind in ["refunds", "returns"]:
                for window in ["baseline", "current"]:
                    item = data["payload"].get("cohort", {}).get(kind, {}).get(window, {})
                    item.pop("order_ids", None)
        return data

    class Meta:
        model = MetricSnapshot
        fields = [
            "id",
            "shop",
            "child_asin",
            "product",
            "fingerprint",
            "rule_version",
            "source_manifest",
            "payload",
            "local_date",
            "created_at",
        ]
        read_only_fields = fields


class EvidenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventEvidence
        fields = ["id", "snapshot", "metric", "detail", "created_at"]
        read_only_fields = fields




class EventSerializer(serializers.ModelSerializer):
    group_title = serializers.CharField(source="group.title", read_only=True, default="")

    class Meta:
        model = BusinessEvent
        fields = [
            "id",
            "shop",
            "child_asin",
            "product",
            "family",
            "group", "group_title",
            "title",
            "status",
            "priority",
            "magnitude",
            "impact",
            "last_cutoff",
            "last_seen",
            "last_checked_at",
            "owner", "closure_reason", "closure_note", "closure_evidence",
            "duplicate_of", "snoozed_until", "snooze_reason",
            "needs_data",
            "consecutive_days",
            "version",
            "created_at",
        ]
        read_only_fields = fields


def scoped(qs, user):
    allowed = products_for(user)
    # An ASIN-wide result can include every SKU. Hide it when even one SKU is outside the current grant.
    visible = Q(pk__in=[])
    for shop in shops_for(user):
        forbidden = (
            Product.objects.filter(shop=shop).exclude(pk__in=allowed.values("pk")).values("child_asin")
        )
        visible |= (
            Q(shop=shop, product__isnull=True, child_asin__in=allowed.filter(shop=shop).values("child_asin"))
            & ~Q(child_asin__in=forbidden)
        )
    return qs.filter(Q(product__in=allowed) | visible)


class SnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SnapshotSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return MetricSnapshot.objects.none()
        qs = scoped(MetricSnapshot.objects.all(), self.request.user).order_by("-created_at")
        if shop := self.request.query_params.get("shop"):
            qs = qs.filter(shop_id=shop)
        if asin := self.request.query_params.get("child_asin"):
            qs = qs.filter(child_asin=asin)
        if "product" in self.request.query_params:
            value = self.request.query_params["product"]
            qs = qs.filter(product_id=None if value == "aggregate" else value)
        return qs

    @extend_schema(request=ScanCommand, responses={202: JobSerializer})
    @action(detail=False, methods=["post"])
    def scan(self, request):
        serializer = ScanCommand(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        require_role(request.user, shop, "operator", "data_admin", "quality", "support")
        authorize_object(request.user, shop, data["child_asin"], data.get("product"))
        payload = {
            "child_asin": data["child_asin"],
            "product_id": str(data["product"]) if data.get("product") else None,
        }
        with transaction.atomic():
            job, created = Job.objects.get_or_create(
                shop=shop,
                actor=request.user,
                kind="scan_metrics",
                idempotency_key=data["idempotency_key"],
                defaults={"payload": payload},
            )
            if job.payload != payload:
                from insight.api_errors import Conflict

                raise Conflict("同一请求标识不能用于不同分析对象。")
            if created:
                Outbox.objects.create(job=job)
                from insight.services import audit

                audit(request.user, shop, "metrics.command_requested", job.pk,
                      after={"kind": job.kind, **payload}, request_id=request.request_id)
        return Response(JobSerializer(job).data, status=202)

    @extend_schema(request=RecomputeCommand, responses={202: JobSerializer})
    @action(detail=False, methods=["post"])
    def recompute(self, request):
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        from django.utils import timezone
        from rest_framework.exceptions import ValidationError

        from insight.api_errors import Conflict

        command = RecomputeCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        require_source_access(request.user, shop)
        authorize_object(request.user, shop, data["child_asin"], data.get("product"))
        today = timezone.now().astimezone(ZoneInfo(shop.timezone)).date()
        if not today - timedelta(days=90) <= data["as_of"] < today:
            raise ValidationError("历史重算日期须在站点最近 90 天内，且早于今日。")
        payload = {
            "child_asin": data["child_asin"],
            "product_id": str(data["product"]) if data.get("product") else None,
            "as_of": str(data["as_of"]),
        }
        with transaction.atomic():
            job, created = Job.objects.get_or_create(
                shop=shop,
                actor=request.user,
                kind="recompute_metrics",
                idempotency_key=data["idempotency_key"],
                defaults={"payload": payload},
            )
            if job.payload != payload:
                raise Conflict("同一请求标识不能用于不同重算范围。")
            if created:
                Outbox.objects.create(job=job)
                from insight.services import audit

                audit(request.user, shop, "metrics.command_requested", job.pk,
                      after={"kind": job.kind, **payload}, request_id=request.request_id)
        return Response(JobSerializer(job).data, status=202)

    @extend_schema(request=RecomputeRangeCommand, responses={202: JobSerializer})
    @action(detail=False, methods=["post"], url_path="recompute-range")
    def recompute_range(self, request):
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        from django.utils import timezone
        from rest_framework.exceptions import ValidationError

        from insight.api_errors import Conflict

        command = RecomputeRangeCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        require_source_access(request.user, shop)
        authorize_object(request.user, shop, data["child_asin"], data.get("product"))
        today = timezone.now().astimezone(ZoneInfo(shop.timezone)).date()
        if data["to_date"] >= today:
            raise ValidationError("历史回补结束日期必须早于站点今日。")
        if data["from_date"] < today - timedelta(days=730):
            raise ValidationError("历史回补最多回看站点今日前 730 天。")
        payload = {
            "child_asin": data["child_asin"],
            "product_id": str(data["product"]) if data.get("product") else None,
            "from_date": str(data["from_date"]),
            "to_date": str(data["to_date"]),
        }
        with transaction.atomic():
            job, created = Job.objects.get_or_create(
                shop=shop,
                actor=request.user,
                kind="recompute_metrics_range",
                idempotency_key=data["idempotency_key"],
                defaults={"payload": payload},
            )
            if {key: value for key, value in job.payload.items() if key != "input_versions"} != payload:
                raise Conflict("同一请求标识不能用于不同历史回补范围。")
            if created:
                Outbox.objects.create(job=job)
                from insight.services import audit

                audit(request.user, shop, "metrics.command_requested", job.pk,
                      after={"kind": job.kind, **payload}, request_id=request.request_id)
        return Response(JobSerializer(job).data, status=202)


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = EventSerializer

    @extend_schema(responses=EventSerializer(many=True))
    @action(detail=True, methods=["get"])
    def related(self, request, pk=None):
        event = self.get_object()
        qs = scoped(BusinessEvent.objects.all(), request.user).filter(
            shop=event.shop, child_asin=event.child_asin, product_id=event.product_id,
        ).exclude(pk=event.pk).select_related("group").order_by("priority", "-last_seen")
        return self.get_paginated_response(EventSerializer(self.paginate_queryset(qs), many=True).data)

    from .grouping import GroupCommand, GroupResult

    @extend_schema(request=GroupCommand, responses=GroupResult)
    @action(detail=True, methods=["post"], url_path="group-command")
    def group_command(self, request, pk=None):
        from .grouping import GroupCommand, group_events

        event = self.get_object()
        command = GroupCommand(data=request.data)
        command.is_valid(raise_exception=True)
        return Response(group_events(request.user, event.pk, command.validated_data, request.request_id))

    @extend_schema(request=EventCommand, responses=EventSerializer)
    @action(detail=True, methods=["post"], url_path="command")
    def command(self, request, pk=None):
        current = self.get_object()
        command = EventCommand(data=request.data)
        command.is_valid(raise_exception=True)
        event = change_event(request.user, current.pk, command.validated_data, request.request_id)
        return Response(self.get_serializer(event).data)

    @transaction.atomic
    @extend_schema(responses={(200, "text/csv"): bytes})
    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        from insight.exports import events_download

        return events_download(request, self.get_queryset())

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return BusinessEvent.objects.none()
        qs = scoped(BusinessEvent.objects.select_related("group"), self.request.user)
        for field in ["shop", "status", "family", "child_asin"]:
            if value := self.request.query_params.get(field):
                qs = qs.filter(**{field: value})
        return qs.order_by(
            "priority",
            Case(When(impact__isnull=True, then=Value(True)), default=Value(False), output_field=BooleanField()),
            F("magnitude").desc(nulls_last=True),
            F("impact").desc(nulls_last=True),
            "-consecutive_days",
            "created_at",
            "id",
        )

    @extend_schema(responses=EvidenceSerializer(many=True))
    @action(detail=True, methods=["get"])
    def evidence(self, request, pk=None):
        qs = self.get_object().evidence.order_by("-created_at")
        return self.get_paginated_response(EvidenceSerializer(self.paginate_queryset(qs), many=True).data)


class ObservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ObservationLead
        fields = [
            "id",
            "shop",
            "child_asin",
            "product",
            "family",
            "metric",
            "title",
            "status",
            "detail",
            "snapshot",
            "last_seen",
            "created_at",
        ]
        read_only_fields = fields


class ObservationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ObservationSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ObservationLead.objects.none()
        qs = scoped(ObservationLead.objects.all(), self.request.user).order_by("-last_seen", "id")
        if shop := self.request.query_params.get("shop"):
            qs = qs.filter(shop=shop)
        if status := self.request.query_params.get("status"):
            qs = qs.filter(status=status)
        return qs


class NotificationSerializer(serializers.ModelSerializer):
    event_title = serializers.SerializerMethodField()

    def get_event_title(self, obj) -> str:
        return obj.event.title if obj.event_id else obj.task.title

    class Meta:
        model = Notification
        fields = ["id", "event", "task", "event_title", "kind", "data_version", "read_at", "created_at"]
        read_only_fields = fields


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = NotificationSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Notification.objects.none()
        qs = (
            Notification.objects.filter(
                Q(event__in=scoped(BusinessEvent.objects.all(), self.request.user)) | Q(task__in=scoped(ActionTask.objects.all(), self.request.user)),
                user=self.request.user
            )
            .select_related("event", "task")
            .order_by("-created_at")
        )
        if shop := self.request.query_params.get("shop"):
            qs = qs.filter(Q(event__shop_id=shop) | Q(task__shop_id=shop))
        if self.request.query_params.get("unread") == "1":
            qs = qs.filter(read_at__isnull=True)
        return qs

    @transaction.atomic
    @extend_schema(request=None, responses=NotificationSerializer)
    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        from django.utils import timezone

        from insight.services import audit

        notification = get_object_or_404(self.get_queryset().select_for_update(of=("self",)), pk=pk)
        if not notification.read_at:
            notification.read_at = timezone.now()
            notification.save(update_fields=["read_at"])
            shop = notification.event.shop if notification.event_id else notification.task.shop
            audit(request.user, shop, "notification.read", notification.pk, after={"read_at": str(notification.read_at)}, request_id=request.request_id)
        return Response(NotificationSerializer(notification).data)


class ScheduleSerializer(serializers.ModelSerializer):
    timezone = serializers.CharField(source="shop.workspace.timezone", read_only=True)

    class Meta:
        from insight.models import DailyScanSchedule

        model = DailyScanSchedule
        fields = ["id", "shop", "enabled", "at", "last_run_date", "version", "error", "timezone"]
        read_only_fields = fields


class ScheduleCommand(serializers.Serializer):
    shop = serializers.UUIDField()
    at = serializers.TimeField()
    enabled = serializers.BooleanField()
    expected_version = serializers.IntegerField(min_value=0)


class ScheduleViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ScheduleSerializer

    def get_queryset(self):
        from insight.models import DailyScanSchedule

        if getattr(self, "swagger_fake_view", False):
            return DailyScanSchedule.objects.none()
        qs = (
            DailyScanSchedule.objects.filter(shop__in=shops_for(self.request.user))
            .select_related("shop__workspace")
            .order_by("id")
        )
        if shop := self.request.query_params.get("shop"):
            qs = qs.filter(shop_id=shop)
        return qs

    @extend_schema(request=ScheduleCommand, responses=ScheduleSerializer)
    def create(self, request):
        from insight.api_errors import Conflict
        from insight.models import DailyScanSchedule, Shop
        from insight.services import audit


        command = ScheduleCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
            Shop.objects.select_for_update().get(pk=shop.pk)
            require_source_access(request.user, shop)
            schedule = DailyScanSchedule.objects.filter(shop=shop).first()
            if data["expected_version"] != (schedule.version if schedule else 0):
                raise Conflict("扫描计划已被修改，请刷新后确认。")
            before = (
                {"enabled": schedule.enabled, "at": str(schedule.at), "version": schedule.version}
                if schedule
                else {}
            )
            if schedule is None:
                schedule = DailyScanSchedule(shop=shop, actor=request.user)
            else:
                schedule.version += 1
            schedule.actor, schedule.at, schedule.enabled, schedule.error = (
                request.user,
                data["at"],
                data["enabled"],
                "",
            )
            schedule.save()
            audit(
                request.user,
                shop,
                "scan.schedule_configured",
                schedule.pk,
                before=before,
                after={"enabled": schedule.enabled, "at": str(schedule.at), "version": schedule.version},
                request_id=request.request_id,
            )
        return Response(ScheduleSerializer(schedule).data)
