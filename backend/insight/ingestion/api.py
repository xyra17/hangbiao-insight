import csv
import io
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from insight.access import shops_for
from insight.models import (
    DirectorySource,
    ImportBatch,
    Job,
    Outbox,
    Product,
    Source,
    SourceContract,
    StagedRow,
)
from insight.serializers import JobSerializer

from .contracts import FACT_MODELS, LABELS, field_catalog
from .service import accept_upload, create_source, require_source_access, revise_contract


class ContractSerializer(serializers.ModelSerializer):
    class Meta:
        model = SourceContract
        fields = [
            "id",
            "version",
            "grain",
            "column_mapping",
            "ignored_columns",
            "approved_at",
            "coverage_basis",
            "return_event",
            "empty_report_means_zero",
            "retention_days",
        ]
        read_only_fields = fields


class DirectorySerializer(serializers.ModelSerializer):
    class Meta:
        model = DirectorySource
        fields = ["enabled", "pattern", "error", "auto_publish", "scan_interval_seconds", "next_scan_at"]
        read_only_fields = fields


class SourceSerializer(serializers.ModelSerializer):
    contracts = ContractSerializer(many=True, read_only=True)
    directory = DirectorySerializer(source="directorysource", read_only=True, allow_null=True)

    class Meta:
        model = Source
        fields = ["id", "shop", "name", "kind", "is_primary", "revision", "contracts", "directory"]
        read_only_fields = fields


class ContractCommand(serializers.Serializer):
    empty_report_means_zero = serializers.BooleanField(default=False)
    grain = serializers.ChoiceField(choices=["asin_day", "product_day", "asin_record", "product_record"])
    column_mapping = serializers.DictField(child=serializers.CharField(max_length=150))
    ignored_columns = serializers.ListField(child=serializers.CharField(max_length=150), default=list)
    expected_version = serializers.IntegerField(min_value=1, required=False)
    coverage_basis = serializers.ChoiceField(
        choices=["business_date", "shipped_date"], default="business_date"
    )
    return_event = serializers.ChoiceField(choices=["received", "requested"], default="received")
    retention_days = serializers.IntegerField(min_value=1, max_value=3650, default=180)


class SourceCommand(ContractCommand):
    shop = serializers.UUIDField()
    name = serializers.CharField(max_length=150)
    kind = serializers.ChoiceField(choices=list(FACT_MODELS))
    is_primary = serializers.BooleanField(default=False)


class BatchSerializer(serializers.ModelSerializer):
    coverage_status = serializers.SerializerMethodField()

    def get_coverage_status(self, obj) -> str | None:
        coverage = getattr(obj, "coveragepartition", None)
        return coverage.status if coverage else None

    source_id = serializers.UUIDField(source="contract.source_id", read_only=True)
    file_hash = serializers.CharField(source="raw_file.sha256", read_only=True)

    class Meta:
        model = ImportBatch
        fields = [
            "id",
            "source_id",
            "contract",
            "filename",
            "file_hash",
            "coverage_start",
            "coverage_end",
            "generated_at",
            "declared_complete",
            "declared_asins",
            "status",
            "total_rows",
            "valid_rows",
            "error_rows",
            "error",
            "published_revision",
            "published_at",
            "revoked_revision",
            "revoked_at",
            "revoke_reason",
            "retracted_rows",
            "created_at",
            "coverage_status",
        ]
        read_only_fields = fields


class RowSerializer(serializers.ModelSerializer):
    class Meta:
        model = StagedRow
        fields = ["id", "line", "raw", "normalized", "business_key", "payload_hash", "error"]
        read_only_fields = fields


class UploadCommand(serializers.Serializer):
    file = serializers.FileField(write_only=True)
    coverage_start = serializers.DateField()
    coverage_end = serializers.DateField()
    generated_at = serializers.DateTimeField()
    declared_complete = serializers.BooleanField(default=False)
    declared_asins = serializers.ListField(
        child=serializers.RegexField(r"^[A-Z0-9]{10}$"), min_length=1, max_length=1000
    )


class PublishCommand(serializers.Serializer):
    allow_partial = serializers.BooleanField(default=False)
    confirm_contract = serializers.BooleanField(default=False)


class RevokeCommand(serializers.Serializer):
    reason = serializers.CharField(min_length=3, max_length=500)
    idempotency_key = serializers.CharField(max_length=150)


def accessible_sources(user):
    result = []
    for shop in shops_for(user):
        try:
            require_source_access(user, shop)
        except Exception as exc:
            from rest_framework.exceptions import PermissionDenied

            if isinstance(exc, PermissionDenied):
                continue
            raise
        result.append(shop.pk)
    return Source.objects.filter(shop_id__in=result)


class SourceViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SourceSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Source.objects.none()
        qs = (
            accessible_sources(self.request.user)
            .select_related("directorysource")
            .prefetch_related("contracts")
            .order_by("name", "id")
        )
        if shop_id := self.request.query_params.get("shop"):
            shop = get_object_or_404(shops_for(self.request.user), pk=shop_id)
            require_source_access(self.request.user, shop)
            qs = qs.filter(shop=shop)
        return qs

    @extend_schema(request=SourceCommand, responses={201: SourceSerializer})
    def create(self, request):
        serializer = SourceCommand(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        source = create_source(request.user, shop, data, request.request_id)
        return Response(SourceSerializer(source).data, status=201)

    @extend_schema(request=ContractCommand, responses=SourceSerializer)
    @action(detail=True, methods=["post"])
    def contracts(self, request, pk=None):
        source = self.get_object()
        serializer = ContractCommand(data=request.data)
        serializer.is_valid(raise_exception=True)
        if "expected_version" not in serializer.validated_data:
            raise ValidationError("请提供当前契约版本。")
        source = revise_contract(request.user, source, serializer.validated_data, request.request_id)
        return Response(SourceSerializer(source).data)

    @extend_schema(
        responses=inline_serializer(
            name="SourceCatalog",
            fields={
                "items": inline_serializer(
                    name="SourceKind",
                    fields={
                        "kind": serializers.CharField(),
                        "label": serializers.CharField(),
                        "fields": inline_serializer(
                            name="SourceField",
                            fields={
                                "name": serializers.CharField(),
                                "required": serializers.BooleanField(),
                                "type": serializers.CharField(),
                                "max_length": serializers.IntegerField(allow_null=True),
                            },
                            many=True,
                        ),
                    },
                    many=True,
                )
            },
        )
    )
    @action(detail=False, methods=["get"])
    def catalog(self, request):
        return Response(
            {
                "items": [
                    {"kind": kind, "label": LABELS[kind], "fields": field_catalog(kind)}
                    for kind in FACT_MODELS
                ]
            }
        )

    @extend_schema(
        responses=inline_serializer(
            name="SourceObjects", fields={"child_asins": serializers.ListField(child=serializers.CharField())}
        )
    )
    @action(detail=True, methods=["get"])
    def objects(self, request, pk=None):
        source = self.get_object()
        return Response(
            {
                "child_asins": list(
                    Product.objects.filter(shop=source.shop)
                    .order_by("child_asin")
                    .values_list("child_asin", flat=True)
                    .distinct()
                )
            }
        )

    @extend_schema(request=UploadCommand, responses={202: BatchSerializer})
    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def upload(self, request, pk=None):
        source = self.get_object()
        serializer = UploadCommand(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        upload = data.pop("file")
        batch = accept_upload(request.user, source, upload, data, request.request_id)
        return Response(BatchSerializer(batch).data, status=202)


class BatchViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BatchSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ImportBatch.objects.none()
        qs = (
            ImportBatch.objects.filter(contract__source__in=accessible_sources(self.request.user))
            .select_related("contract", "raw_file", "coveragepartition")
            .order_by("-created_at", "id")
        )
        if source := self.request.query_params.get("source"):
            qs = qs.filter(contract__source_id=source)
        return qs

    @extend_schema(responses=RowSerializer(many=True))
    @action(detail=True, methods=["get"])
    def rows(self, request, pk=None):
        batch = self.get_object()
        qs = batch.rows.order_by("line")
        if request.query_params.get("errors") == "1":
            qs = qs.exclude(error="")
        return self.get_paginated_response(RowSerializer(self.paginate_queryset(qs), many=True).data)

    @extend_schema(responses={(200, "application/octet-stream"): bytes})
    @action(detail=True, methods=["get"], url_path="original-file")
    def original_file(self, request, pk=None):
        batch = self.get_object()
        if batch.raw_file.deleted_at:
            return HttpResponse("原始文件已按保留期清理，校验值和审计占位仍可追溯。", status=410)
        from insight.services import audit

        stream = (Path(settings.STORAGE_ROOT) / batch.raw_file.storage_key).open("rb")
        try:
            audit(
                request.user,
                batch.contract.source.shop,
                "import.original_downloaded",
                batch.pk,
                after={"sha256": batch.raw_file.sha256},
                request_id=request.request_id,
            )
        except Exception:
            stream.close()
            raise
        return FileResponse(
            stream, as_attachment=True, filename=batch.filename, content_type="application/octet-stream"
        )

    @extend_schema(request=PublishCommand, responses={202: JobSerializer})
    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        batch = self.get_object()
        serializer = PublishCommand(data=request.data)
        serializer.is_valid(raise_exception=True)
        require_source_access(request.user, batch.contract.source.shop)
        from .contracts import digest

        with transaction.atomic():
            job, created = Job.objects.get_or_create(
                shop=batch.contract.source.shop,
                actor=request.user,
                kind="publish_import",
                idempotency_key=digest({"batch": str(batch.pk), **serializer.validated_data}),
                defaults={"payload": {"batch_id": str(batch.pk), **serializer.validated_data}},
            )
            if created:
                Outbox.objects.create(job=job)
        return Response(JobSerializer(job).data, status=202)

    @extend_schema(request=RevokeCommand, responses={202: JobSerializer})
    @action(detail=True, methods=["post"], url_path="revoke-publication")
    def revoke_publication(self, request, pk=None):
        """Create a queued correction command for the current publication."""
        batch = self.get_object()
        serializer = RevokeCommand(data=request.data)
        serializer.is_valid(raise_exception=True)
        require_source_access(request.user, batch.contract.source.shop)
        from .contracts import digest

        key = digest({"batch": str(batch.pk), **serializer.validated_data})
        with transaction.atomic():
            job, created = Job.objects.get_or_create(
                shop=batch.contract.source.shop,
                actor=request.user,
                kind="revoke_import",
                idempotency_key=key,
                defaults={"payload": {"batch_id": str(batch.pk), **serializer.validated_data}},
            )
            if created:
                Outbox.objects.create(job=job)
        return Response(JobSerializer(job).data, status=202)

    @extend_schema(responses={(200, "text/csv"): str})
    @action(detail=True, methods=["get"], url_path="error-file")
    def error_file(self, request, pk=None):
        batch = self.get_object()
        handle = io.StringIO(newline="")
        writer = csv.writer(handle)
        writer.writerow(["line", "error"])
        for row in batch.rows.exclude(error="").order_by("line").iterator():
            # No raw cell content is placed in an error export, preventing formula injection/PII leakage.
            writer.writerow(
                [row.line, "'" + row.error if row.error.startswith(("=", "+", "-", "@")) else row.error]
            )
        response = HttpResponse("\ufeff" + handle.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="import-errors.csv"'
        return response
