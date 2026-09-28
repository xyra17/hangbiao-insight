from copy import deepcopy

from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from insight.access import require_role, shops_for
from insight.api_errors import Conflict
from insight.models import Job, Outbox, ParentSnapshot
from insight.serializers import JobSerializer

from .api import can_read_raw_source
from .parent import authorize_parent


class ParentCommand(serializers.Serializer):
    shop = serializers.UUIDField()
    parent_asin = serializers.RegexField(r"^[A-Z0-9]{10}$")
    idempotency_key = serializers.CharField(max_length=150)


class ParentSnapshotSerializer(serializers.ModelSerializer):
    payload = serializers.DictField(child=serializers.JSONField(), read_only=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not can_read_raw_source(self.context["request"].user, instance.shop):
            data["payload"] = deepcopy(data["payload"])
            sections = [data["payload"].get("cohort", {})] + list(
                data["payload"].get("children", {}).values()
            )
            for section in sections:
                for kind in ("refunds", "returns"):
                    for window in ("baseline", "current"):
                        section.get(kind, {}).get(window, {}).pop("order_ids", None)
        return data

    class Meta:
        model = ParentSnapshot
        fields = [
            "id",
            "shop",
            "parent_asin",
            "fingerprint",
            "rule_version",
            "source_manifest",
            "mapping_versions",
            "payload",
            "local_date",
            "created_at",
        ]
        read_only_fields = fields


class ParentSnapshotViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = ParentSnapshotSerializer
    queryset = ParentSnapshot.objects.none()

    def get_object(self):
        instance = get_object_or_404(
            ParentSnapshot.objects.select_related("shop"),
            pk=self.kwargs["pk"],
            shop__in=shops_for(self.request.user),
        )
        try:
            authorize_parent(
                self.request.user, instance.shop, instance.parent_asin, instance.mapping_versions
            )
        except (PermissionDenied, ValidationError):
            raise Http404
        return instance

    @extend_schema(request=ParentCommand, responses={202: JobSerializer})
    def create(self, request):
        command = ParentCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        require_role(request.user, shop, "operator", "data_admin", "quality", "support")
        authorize_parent(request.user, shop, data["parent_asin"])
        payload = {"parent_asin": data["parent_asin"]}
        with transaction.atomic():
            job, created = Job.objects.get_or_create(
                shop=shop,
                actor=request.user,
                kind="summarize_parent",
                idempotency_key=data["idempotency_key"],
                defaults={"payload": payload},
            )
            if job.payload != payload:
                raise Conflict("同一请求标识不能用于不同父体。")
            if created:
                Outbox.objects.create(job=job)
        return Response(JobSerializer(job).data, status=202)
