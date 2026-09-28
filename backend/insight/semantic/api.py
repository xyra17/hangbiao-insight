from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from insight.access import require_role, shops_for
from insight.api_errors import Conflict
from insight.ingestion.service import require_source_access
from insight.metrics.api import scoped
from insight.metrics.facts import authorize_object
from insight.models import Job, Outbox, SemanticClaim, SemanticRun, StandardReport
from insight.serializers import JobSerializer


class SemanticRunCommand(serializers.Serializer):
    shop = serializers.UUIDField()
    child_asin = serializers.RegexField(r"^[A-Z0-9]{10}$")
    product = serializers.UUIDField(required=False, allow_null=True)
    provider = serializers.ChoiceField(choices=["deterministic", "deepseek", "lmstudio", "qwen"])
    idempotency_key = serializers.CharField(max_length=150)


class SemanticClaimSerializer(serializers.ModelSerializer):
    class Meta:
        model = SemanticClaim
        fields = [
            "id",
            "review",
            "prediction_id",
            "primary_topic",
            "secondary_topic",
            "polarity",
            "evidence_span",
            "prediction_status",
            "quality_flags",
            "provider",
            "model_name",
            "prompt_version",
            "taxonomy_version",
        ]
        read_only_fields = fields


class SemanticRunSerializer(serializers.ModelSerializer):
    claims = SemanticClaimSerializer(many=True, read_only=True)

    class Meta:
        model = SemanticRun
        fields = [
            "id",
            "shop",
            "child_asin",
            "product",
            "status",
            "provider",
            "model_name",
            "prompt_version",
            "taxonomy_version",
            "source_manifest",
            "total_reviews",
            "accepted_claims",
            "rejected_claims",
            "error_count",
            "error",
            "finished_at",
            "created_at",
            "claims",
        ]
        read_only_fields = fields


class SemanticRunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SemanticRunSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SemanticRun.objects.none()
        qs = scoped(SemanticRun.objects.all(), self.request.user)
        if shop := self.request.query_params.get("shop"):
            qs = qs.filter(shop_id=shop)
        return qs.prefetch_related("claims").order_by("-created_at")

    @extend_schema(request=SemanticRunCommand, responses={202: JobSerializer})
    def create(self, request):
        command = SemanticRunCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        require_role(request.user, shop, "operator", "data_admin", "quality", "support")
        require_source_access(request.user, shop)
        authorize_object(request.user, shop, data["child_asin"], data.get("product"))
        payload = {
            "child_asin": data["child_asin"],
            "product_id": str(data["product"]) if data.get("product") else None,
            "provider": data["provider"],
        }
        with transaction.atomic():
            job, created = Job.objects.get_or_create(
                shop=shop,
                actor=request.user,
                kind="semantic_run",
                idempotency_key=data["idempotency_key"],
                defaults={"payload": payload},
            )
            if job.payload != payload:
                raise Conflict("同一请求标识不能用于不同语义范围。")
            if created:
                Outbox.objects.create(job=job)
        return Response(JobSerializer(job).data, status=202)


class ReportSerializer(serializers.ModelSerializer):
    refreshes = serializers.SerializerMethodField()

    def get_refreshes(self, obj) -> list[dict]:
        from insight.derived import artifact_refreshes
        return artifact_refreshes("report", obj.pk)

    class Meta:
        model = StandardReport
        fields = [
            "id", "refreshes",
            "shop",
            "child_asin",
            "product",
            "metric_snapshot",
            "semantic_run",
            "report_version",
            "previous_report",
            "input_versions",
            "status",
            "payload",
            "publication_key",
            "created_at",
        ]
        read_only_fields = fields


class ReportViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ReportSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return StandardReport.objects.none()
        qs = scoped(StandardReport.objects.all(), self.request.user)
        if shop := self.request.query_params.get("shop"):
            qs = qs.filter(shop_id=shop)
        return qs.order_by("-created_at")

    @transaction.atomic
    @extend_schema(responses={(200, "application/json"): bytes, (200, "text/html"): bytes})
    @action(detail=True, methods=["get"], url_path="export")
    def export(self, request, pk=None):
        from insight.exports import report_download

        return report_download(request, self.get_object(), request.query_params.get("file_type", "html"))


class EvidenceSearchCommand(serializers.Serializer):
    shop = serializers.UUIDField()
    child_asin = serializers.RegexField(r"^[A-Z0-9]{10}$")
    product = serializers.UUIDField(required=False, allow_null=True)
    query = serializers.CharField(min_length=1, max_length=200)
    start = serializers.DateField()
    end = serializers.DateField()
    kind = serializers.ChoiceField(choices=["reviews", "support"], default="reviews")
    limit = serializers.IntegerField(min_value=1, max_value=50, default=10)
    language = serializers.CharField(max_length=20, required=False)
    topic = serializers.CharField(max_length=100, required=False)
    polarity = serializers.ChoiceField(choices=["positive", "negative", "neutral", "mixed"], required=False)

    def validate(self, attrs):
        if attrs["start"] > attrs["end"]:
            raise serializers.ValidationError("开始日期不能晚于结束日期。")
        return attrs


class EvidenceSearchResult(serializers.Serializer):
    retrieval_id = serializers.UUIDField()
    retrieval_version = serializers.CharField()
    alias_version = serializers.CharField()
    index_version = serializers.CharField()
    source_manifest = serializers.DictField()
    scope = serializers.DictField()
    query = serializers.CharField()
    items = serializers.ListField(child=serializers.DictField())
    limitations = serializers.ListField(child=serializers.CharField())


class EvidenceSearchView(APIView):
    @extend_schema(request=EvidenceSearchCommand, responses=EvidenceSearchResult)
    def post(self, request):
        from .retrieval import retrieve

        command = EvidenceSearchCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        return Response(
            retrieve(
                request.user,
                shop,
                data["child_asin"],
                query=data["query"],
                start=data["start"],
                end=data["end"],
                product_id=data.get("product"),
                kind=data["kind"],
                limit=data["limit"],
                language=data.get("language"), topic=data.get("topic"), polarity=data.get("polarity"),
            )
        )
