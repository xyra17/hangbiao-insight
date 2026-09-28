from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from insight.access import require_role, shops_for
from insight.api_errors import Conflict
from insight.metrics.facts import authorize_object
from insight.models import InvestigationRun, InvestigationStep, Job, Outbox
from insight.serializers import JobSerializer
from insight.services import audit

from .access import visible_investigations


class InvestigationCommand(serializers.Serializer):
    shop = serializers.UUIDField()
    query = serializers.CharField(min_length=2, max_length=2000)
    child_asin = serializers.RegexField(r"^[A-Z0-9]{10}$", required=False, allow_blank=True)
    product = serializers.UUIDField(required=False, allow_null=True)
    max_steps = serializers.IntegerField(min_value=3, max_value=12, default=6)
    allow_internal_task = serializers.BooleanField(
        default=False,
        help_text="明确允许调查图在有证据时创建一个未指派的内部核查草稿；不授权任何外部经营变更。",
    )
    idempotency_key = serializers.CharField(max_length=150)


class InvestigationStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvestigationStep
        fields = [
            "id",
            "sequence",
            "node",
            "tool_name",
            "status",
            "input",
            "output",
            "evidence_refs",
            "created_at",
        ]
        read_only_fields = fields


class InvestigationSerializer(serializers.ModelSerializer):
    steps = InvestigationStepSerializer(many=True, read_only=True)
    refreshes = serializers.SerializerMethodField()

    def get_refreshes(self, obj) -> list[dict]:
        from insight.derived import artifact_refreshes
        return artifact_refreshes("investigation", obj.pk)


    class Meta:
        model = InvestigationRun
        fields = [
            "id",
            "shop",
            "requested_by",
            "query",
            "child_asin",
            "product",
            "status",
            "route",
            "max_steps",
            "steps_used",
            "budget_units",
            "stop_reason",
            "state",
            "result",
            "checkpoint_version",
            "input_versions", "previous_run", "read_only_refresh", "refreshes",
            "created_at",
            "steps",
        ]
        read_only_fields = fields


class InvestigationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = InvestigationSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return InvestigationRun.objects.none()
        qs = visible_investigations(self.request.user)
        if shop := self.request.query_params.get("shop"):
            qs = qs.filter(shop_id=shop)
        return qs.prefetch_related("steps").order_by("-created_at")

    @extend_schema(request=InvestigationCommand, responses={202: JobSerializer})
    def create(self, request):
        command = InvestigationCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        require_role(request.user, shop, "operator", "data_admin", "quality", "support")
        if data.get("child_asin"):
            authorize_object(request.user, shop, data["child_asin"], data.get("product"))
        payload = {
            "query": data["query"],
            "child_asin": data.get("child_asin", ""),
            "product_id": str(data["product"]) if data.get("product") else None,
            "max_steps": data["max_steps"],
            "allow_internal_task": data["allow_internal_task"],
        }
        existing_job = Job.objects.filter(
            shop=shop, actor=request.user, kind="investigation_run", idempotency_key=data["idempotency_key"]
        ).first()
        if existing_job:
            if {key: value for key, value in existing_job.payload.items() if key != "run_id"} != payload:
                raise Conflict("同一请求标识不能用于不同调查范围。")
            return Response(JobSerializer(existing_job).data, status=202)
        with transaction.atomic():
            # Serialize commands for this shop; distinct explicit requests get new runs.
            type(shop).objects.select_for_update().get(pk=shop.pk)
            existing_job = Job.objects.filter(
                shop=shop,
                actor=request.user,
                kind="investigation_run",
                idempotency_key=data["idempotency_key"],
            ).first()
            if existing_job:
                if {k: v for k, v in existing_job.payload.items() if k != "run_id"} != payload:
                    raise Conflict("同一请求标识不能用于不同调查范围。")
                return Response(JobSerializer(existing_job).data, status=202)
            if InvestigationRun.objects.filter(
                shop=shop,
                requested_by=request.user,
                query=data["query"],
                child_asin=data.get("child_asin", ""),
                product_id=data.get("product"),
                status__in=["queued", "running"],
            ).exists():
                raise Conflict("相同调查正在执行，请查看已有运行记录。")
            run = InvestigationRun.objects.create(
                shop=shop,
                requested_by=request.user,
                query=data["query"],
                child_asin=data.get("child_asin", ""),
                product_id=data.get("product"),
                max_steps=data["max_steps"],
                budget_units=data["max_steps"],
                state={"write_requested": True} if data["allow_internal_task"] else {},
            )
            job_payload = {**payload, "run_id": str(run.pk)}
            job = Job.objects.create(
                shop=shop,
                actor=request.user,
                kind="investigation_run",
                idempotency_key=data["idempotency_key"],
                payload=job_payload,
                result={"investigation_id": str(run.pk), "status": "queued"},
            )
            Outbox.objects.create(job=job)
            audit(request.user, shop, "investigation.created", run.pk,
                  after={"job_id": str(job.pk), "max_steps": run.max_steps}, request_id=request.request_id)
        return Response(JobSerializer(job).data, status=202)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Stop a run at the next graph checkpoint; no budget is refunded."""
        with transaction.atomic():
            run = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            require_role(request.user, run.shop, "operator", "data_admin", "quality", "support")
            if run.status in {"completed", "needs_clarification", "cancelled"}:
                return Response(self.get_serializer(run).data)
            run.status, run.stop_reason = "cancelled", "由发起人取消。"
            run.checkpoint_version += 1
            run.save(update_fields=["status", "stop_reason", "checkpoint_version"])
            audit(request.user, run.shop, "investigation.cancelled", run.pk,
                  after={"status": run.status}, request_id=request.request_id)
        return Response(self.get_serializer(run).data)

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        """Create one resumable job while preserving steps and the original budget."""
        import uuid

        with transaction.atomic():
            run = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            require_role(request.user, run.shop, "operator", "data_admin", "quality", "support")
            if run.status not in {"cancelled", "partial"}:
                raise Conflict("只有已取消或部分完成的调查可以接续。")
            if run.steps.filter(logical_key="").exists():
                raise Conflict("旧调查没有完整的冻结与恢复记录，请发起新调查。")
            if run.steps_used >= run.max_steps:
                raise Conflict("调查累计步骤额度已用完，接续不会重置预算。")
            if run.child_asin:
                authorize_object(request.user, run.shop, run.child_asin, run.product_id)
            run.status, run.stop_reason = "queued", ""
            run.save(update_fields=["status", "stop_reason"])
            job = Job.objects.create(
                shop=run.shop,
                actor=request.user,
                kind="investigation_run",
                result={"investigation_id": str(run.pk), "status": "queued"},
                idempotency_key=f"resume-{run.pk}-{uuid.uuid4().hex}",
                payload={
                    "query": run.query,
                    "child_asin": run.child_asin,
                    "product_id": str(run.product_id) if run.product_id else None,
                    "max_steps": run.max_steps,
                    "allow_internal_task": bool(run.state.get("write_requested")),
                    "run_id": str(run.pk),
                },
            )
            Outbox.objects.create(job=job)
            audit(request.user, run.shop, "investigation.resumed", run.pk,
                  after={"job_id": str(job.pk), "steps_used": run.steps_used}, request_id=request.request_id)
        return Response(JobSerializer(job).data, status=202)
