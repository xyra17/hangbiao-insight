from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from app.agent.output_contract import BUSINESS_JSON_SCHEMA, LEGACY_BUSINESS_JSON_SCHEMA
from insight.access import require_role, shops_for
from insight.api_errors import Conflict
from insight.ingestion.contracts import digest
from insight.metrics.facts import authorize_object
from insight.models import InvestigationRun, InvestigationStep, Job, Outbox
from insight.serializers import JobSerializer
from insight.services import audit

from .access import visible_investigations
from .policy import permission_matrix


class InvestigationCommand(serializers.Serializer):
    shop = serializers.UUIDField()
    query = serializers.CharField(min_length=2, max_length=2000)
    child_asin = serializers.RegexField(r"^[A-Z0-9]{10}$", required=False, allow_blank=True)
    product = serializers.UUIDField(required=False, allow_null=True)
    max_steps = serializers.IntegerField(min_value=3, max_value=12, default=6)
    planner_provider = serializers.ChoiceField(
        choices=["deterministic", "deepseek"],
        default="deterministic",
        help_text="默认使用确定性 planner；真实供应商须通过部署环境明确启用。",
    )
    allow_internal_task = serializers.BooleanField(
        default=False,
        help_text="明确允许调查图在有证据时创建一个未指派的内部核查草稿；不授权任何外部经营变更。",
    )
    idempotency_key = serializers.CharField(max_length=150)


class InvestigationClarification(serializers.Serializer):
    query = serializers.CharField(min_length=2, max_length=2000)
    child_asin = serializers.RegexField(r"^[A-Z0-9]{10}$", required=False, allow_blank=True)
    product = serializers.UUIDField(required=False, allow_null=True)
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


class InvestigationConclusion(serializers.Serializer):
    status = serializers.ChoiceField(choices=["unknown", "pending_adjudication"], required=False)
    statement = serializers.CharField(required=False)
    proposed_statement = serializers.CharField(required=False)
    citation_status = serializers.CharField(required=False)
    citations = serializers.ListField(child=serializers.DictField(), required=False)
    semantic_judgment = serializers.CharField(required=False)


class InvestigationObservation(serializers.Serializer):
    fact_id = serializers.CharField(required=False)
    record_id = serializers.CharField(required=False)
    raw_row_id = serializers.CharField(required=False)
    business_date = serializers.CharField(required=False)
    revision = serializers.IntegerField(required=False)
    title = serializers.CharField(required=False, allow_blank=True)
    text = serializers.CharField(required=False)
    match_kind = serializers.ChoiceField(choices=["relevant_hit", "scope_context"], required=False)
    evidence_contexts = serializers.ListField(child=serializers.DictField(), required=False)
    evidence_relation = serializers.CharField(required=False)
    source_kind = serializers.CharField(required=False, allow_null=True)
    phase = serializers.CharField(required=False)




@extend_schema_field({"oneOf": [LEGACY_BUSINESS_JSON_SCHEMA, BUSINESS_JSON_SCHEMA]})
class BusinessContractField(serializers.DictField):
    """Native-v2 arrays and legacy-v1 strings remain explicitly versioned."""


class InvestigationOutput(serializers.Serializer):
    business_schema_version = serializers.CharField(required=False)
    business_contract = BusinessContractField(required=False)
    business_contract_registry_sha256 = serializers.CharField(required=False)
    business_contract_validation = serializers.DictField(required=False)
    output_schema_version = serializers.CharField(required=False)
    conclusion = InvestigationConclusion(required=False)
    observations = InvestigationObservation(many=True, required=False)
    retrieval_summary = serializers.DictField(child=serializers.IntegerField(), required=False)
    request_scope = serializers.DictField(required=False, allow_null=True)
    evidence = serializers.ListField(child=serializers.DictField(), required=False)
    hypotheses = serializers.ListField(child=serializers.DictField(), required=False)
    limitations = serializers.ListField(child=serializers.CharField(), required=False)
    clarification = serializers.CharField(required=False)
    write = serializers.DictField(required=False, allow_null=True)


class InvestigationSerializer(serializers.ModelSerializer):
    result = serializers.SerializerMethodField()
    steps = InvestigationStepSerializer(many=True, read_only=True)
    refreshes = serializers.SerializerMethodField()
    budget = serializers.SerializerMethodField()
    tool_calls = serializers.SerializerMethodField()
    tool_permissions = serializers.SerializerMethodField()

    def to_representation(self, instance):
        from insight.metrics.api import can_read_raw_source

        from .orders import redact_order_ids
        data = super().to_representation(instance)
        return data if can_read_raw_source(self.context["request"].user, instance.shop) else redact_order_ids(data)

    @extend_schema_field(InvestigationOutput)
    def get_result(self, obj):
        return obj.result

    def get_refreshes(self, obj) -> list[dict]:
        from insight.derived import artifact_refreshes
        return artifact_refreshes("investigation", obj.pk)

    def get_budget(self, obj) -> dict:
        entries = list(obj.budget_ledger.order_by("sequence"))
        return {
            "max_steps": obj.max_steps,
            "steps_used": obj.steps_used,
            "remaining_units": obj.budget_units,
            "model_attempts": obj.model_attempts,
            "model_tokens": obj.model_tokens,
            "model_cost": str(obj.model_cost),
            "elapsed_ms": obj.elapsed_ms,
            "duplicate_queries": obj.duplicate_queries,
            "entries": [
                {
                    "sequence": item.sequence,
                    "kind": item.kind,
                    "operation": item.operation,
                    "used_units": item.used_units,
                    "tokens": item.tokens,
                    "cost": str(item.cost),
                    "duration_ms": item.duration_ms,
                    "metadata": item.metadata,
                }
                for item in entries
            ],
        }

    def get_tool_calls(self, obj) -> list[dict]:
        return [
            {
                "sequence": call.sequence,
                "tool_name": call.tool_name,
                "scope": call.scope,
                "authorization": call.authorization,
                "grant_version": call.grant_version,
                "status": call.status,
                "output_refs": call.output_refs,
                "error": call.error,
            }
            for call in obj.tool_calls.order_by("sequence")
        ]

    def get_tool_permissions(self, obj) -> list[dict]:
        """Expose the frozen matrix so a reviewer can audit available tools."""

        return permission_matrix()


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
            "input_versions", "previous_run", "read_only_refresh", "refreshes", "budget", "tool_calls",
            "tool_permissions",
            "model_attempts", "model_tokens", "model_cost", "elapsed_ms", "duplicate_queries",
            "decisions", "counter_evidence",
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
            "planner_provider": data["planner_provider"],
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
            run.state = {
                **run.state,
                "planner_provider": data["planner_provider"],
            }
            run.save(update_fields=["state"])
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
                    "planner_provider": run.state.get("planner_provider", "deterministic"),
                    "run_id": str(run.pk),
                },
            )
            Outbox.objects.create(job=job)
            audit(request.user, run.shop, "investigation.resumed", run.pk,
                  after={"job_id": str(job.pk), "steps_used": run.steps_used}, request_id=request.request_id)
        return Response(JobSerializer(job).data, status=202)

    @extend_schema(request=InvestigationClarification, responses={202: JobSerializer})
    @action(detail=True, methods=["post"])
    def clarify(self, request, pk=None):
        """Continue a clarification as a linked run using only remaining budget."""
        command = InvestigationClarification(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            run = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            require_role(request.user, run.shop, "operator", "data_admin", "quality", "support")
            if run.status != "needs_clarification":
                raise Conflict("只有等待定位澄清的调查可以补充范围。")
            remaining = max(run.max_steps - run.steps_used, 0)
            if remaining < 3:
                raise Conflict("当前调查剩余预算不足以继续，请发起新的调查并重新确认范围。")
            date_correction = (run.result.get("request_scope") or {}).get("status") == "needs_clarification"
            child_asin = data.get("child_asin") or (run.child_asin if date_correction else "")
            product_id = data.get("product", run.product_id if date_correction and child_asin == run.child_asin else None)
            if child_asin:
                authorize_object(request.user, run.shop, child_asin, product_id)
            command_scope = {"query": data["query"], "child_asin": child_asin,
                             "product_id": str(product_id) if product_id else None, "previous_run": str(run.pk)}
            command_key = "clarify-" + digest({"parent": str(run.pk), "key": data["idempotency_key"]})
            existing = Job.objects.filter(shop=run.shop, actor=request.user, kind="investigation_run",
                                          idempotency_key=command_key).first()
            if existing:
                if existing.payload.get("clarification_request") != command_scope:
                    raise Conflict("同一澄清请求标识不能用于不同日期或对象。")
                return Response(JobSerializer(existing).data, status=202)
            child = InvestigationRun.objects.create(
                previous_run=run,
                shop=run.shop,
                requested_by=request.user,
                query=f"{run.query}\n补充定位：{data['query']}",
                child_asin=child_asin,
                product_id=product_id,
                max_steps=remaining,
                budget_units=remaining,
                state={
                    "clarified_from": str(run.pk),
                    "original_query": run.query,
                    **({"scope_query": data["query"]} if date_correction else {}),
                    "planner_provider": run.state.get("planner_provider", "deterministic"),
                    "write_requested": bool(run.state.get("write_requested")),
                },
            )
            job = Job.objects.create(
                shop=run.shop,
                actor=request.user,
                kind="investigation_run",
                idempotency_key=command_key,
                payload={
                    "clarification_request": command_scope,
                    "query": child.query,
                    "child_asin": child.child_asin,
                    "product_id": str(child.product_id) if child.product_id else None,
                    "max_steps": child.max_steps,
                    "allow_internal_task": bool(run.state.get("write_requested")),
                    "planner_provider": run.state.get("planner_provider", "deterministic"),
                    "run_id": str(child.pk),
                },
                result={"investigation_id": str(child.pk), "status": "queued", "previous_run": str(run.pk)},
            )
            Outbox.objects.create(job=job)
            audit(request.user, run.shop, "investigation.clarified", child.pk,
                  after={"previous_run": str(run.pk), "steps_remaining": remaining}, request_id=request.request_id)
        return Response(JobSerializer(job).data, status=202)
