from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from insight.access import require_role, shops_for
from insight.api_errors import Conflict
from insight.evidence_refs import validate_evidence_refs
from insight.metrics.api import scoped
from insight.metrics.facts import authorize_object
from insight.models import (
    ActionExecution,
    ActionTask,
    BusinessEvent,
    ChangeProposal,
    InvestigationRun,
    Notification,
    StandardReport,
)
from insight.proposals import execute_proposal, payload_hash, policy_description, validate_internal_proposal
from insight.services import audit


class ActionTaskSerializer(serializers.ModelSerializer):
    source_lineage = serializers.SerializerMethodField()

    def get_source_lineage(self, task):
        if not task.source_report_id and not task.source_investigation_id:
            return None
        report_versions = task.source_report.input_versions if task.source_report_id else {}
        investigation_versions = task.source_investigation.input_versions if task.source_investigation_id else {}
        return {
            "source_manifest": report_versions.get("source_manifest") or investigation_versions.get("source_manifest", {}),
            "metric_snapshot": report_versions.get("metric_snapshot") or investigation_versions.get("metric_snapshot"),
            "mapping_versions": report_versions.get("mapping_versions") or investigation_versions.get("mapping_versions", {}),
        }

    class Meta:
        model = ActionTask
        fields = [
            "id",
            "shop",
            "event",
            "source_report",
            "source_investigation",
            "human_confirmed_by",
            "human_confirmed_at",
            "human_confirmation_note",
            "source_lineage",
            "child_asin",
            "product",
            "title",
            "status",
            "assigned_to",
            "created_by",
            "due_at",
            "review_at",
            "review_status",
            "review_result",
            "review_comparison",
            "action_type", "question", "completion_criteria", "evidence_refs",
            "observed_metrics", "effective_at", "is_formal",
            "version",
            "created_at",
        ]
        read_only_fields = ["id", "created_by", "version", "created_at"]


class ActionDetails(serializers.Serializer):
    action_type = serializers.ChoiceField(choices=["investigation", "listing", "quality", "support", "inventory", "other"], default="investigation")
    question = serializers.CharField(max_length=3000, required=False, allow_blank=True)
    completion_criteria = serializers.CharField(max_length=3000, required=False, allow_blank=True)
    evidence_refs = serializers.ListField(child=serializers.CharField(max_length=200), required=False)
    observed_metrics = serializers.ListField(child=serializers.ChoiceField(choices=["ordered_units", "sessions", "unit_session_percent", "refunds", "returns"]), required=False)
    is_formal = serializers.BooleanField(default=False)
    confirm_source = serializers.BooleanField(default=False)
    confirmation_note = serializers.CharField(max_length=1000, required=False, allow_blank=True)

    def validate(self, data):
        refs = data.get("evidence_refs", [])
        validate_evidence_refs(refs)
        if data.get("is_formal") and not all(data.get(field) for field in ("assigned_to", "due_at", "question", "completion_criteria", "observed_metrics")):
            raise serializers.ValidationError("正式动作需要负责人、截止日期、核查问题、完成标准和观察指标。")
        has_report = bool(data.get("source_report"))
        has_investigation = bool(data.get("source_investigation"))
        if has_investigation and not has_report:
            raise serializers.ValidationError("调查来源必须与其报告一起绑定。")
        if has_report and has_investigation and (not data.get("confirm_source") or not data.get("confirmation_note", "").strip()):
            raise serializers.ValidationError("绑定报告与调查前必须由操作者明确确认并填写确认说明。")
        if data.get("confirm_source") and not (has_report and has_investigation):
            raise serializers.ValidationError("来源确认需要同时指定报告和调查。")
        return data


class ActionTaskCommand(ActionDetails):
    source_report = serializers.UUIDField(required=False, allow_null=True)
    source_investigation = serializers.UUIDField(required=False, allow_null=True)
    shop = serializers.UUIDField()
    child_asin = serializers.RegexField(r"^[A-Z0-9]{10}$")
    product = serializers.UUIDField(required=False, allow_null=True)
    event = serializers.UUIDField(required=False, allow_null=True)
    title = serializers.CharField(max_length=240)
    assigned_to = serializers.IntegerField(required=False, allow_null=True)
    due_at = serializers.DateTimeField(required=False, allow_null=True)
    expected_version = serializers.IntegerField(min_value=1, required=False)


class DraftRevisionCommand(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)
    title = serializers.CharField(max_length=240, required=False)
    action_type = serializers.ChoiceField(
        choices=["investigation", "listing", "quality", "support", "inventory", "other"], required=False
    )
    question = serializers.CharField(max_length=3000, required=False, allow_blank=True)
    completion_criteria = serializers.CharField(max_length=3000, required=False, allow_blank=True)
    evidence_refs = serializers.ListField(child=serializers.CharField(max_length=200), required=False)
    observed_metrics = serializers.ListField(
        child=serializers.ChoiceField(choices=["ordered_units", "sessions", "unit_session_percent", "refunds", "returns"]),
        required=False,
    )

    def validate(self, data):
        if len(data) == 1:
            raise serializers.ValidationError("草稿修订至少需要一个变更字段。")
        refs = data.get("evidence_refs", [])
        validate_evidence_refs(refs)
        return data


class ExecutionCommand(serializers.Serializer):
    effective_at = serializers.DateTimeField(required=False)
    review_at = serializers.DateTimeField(required=False)
    observed_metrics = serializers.ListField(child=serializers.ChoiceField(choices=["ordered_units", "sessions", "unit_session_percent", "refunds", "returns"]), required=False)
    notes = serializers.CharField(min_length=1, max_length=5000)
    evidence_refs = serializers.ListField(child=serializers.CharField(max_length=200), required=False)
    expected_version = serializers.IntegerField(min_value=1)

    def validate(self, data):
        validate_evidence_refs(data.get("evidence_refs", []))
        return data


class ReviewCommand(serializers.Serializer):
    review_at = serializers.DateTimeField(required=False, allow_null=True)
    result = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    snapshot = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(choices=["scheduled", "completed", "not_scheduled"])
    expected_version = serializers.IntegerField(min_value=1)


class ActionTaskViewSet(viewsets.ModelViewSet):
    serializer_class = ActionTaskSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ActionTask.objects.none()
        qs = scoped(ActionTask.objects.all(), self.request.user)
        if shop := self.request.query_params.get("shop"):
            qs = qs.filter(shop_id=shop)
        if event := self.request.query_params.get("event"):
            qs = qs.filter(event_id=event)
        # Keep newly created drafts and recent work at the front of the table.
        # Sorting by status/due date can bury a just-created task behind old
        # open tasks, making the post-create workflow appear to lose it.
        return qs.order_by("-created_at", "-id")

    @transaction.atomic
    @extend_schema(request=ActionTaskCommand, responses={201: ActionTaskSerializer})
    def create(self, request, *args, **kwargs):
        command = ActionTaskCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        require_role(request.user, shop, "operator", "quality", "support", "data_admin")
        authorize_object(request.user, shop, data["child_asin"], data.get("product"))
        if data.get("source_report"):
            report = get_object_or_404(scoped(StandardReport.objects.all(), request.user), pk=data["source_report"])
            if (report.shop_id, report.child_asin, report.product_id) != (shop.pk, data["child_asin"], data.get("product")):
                raise serializers.ValidationError("动作必须绑定同一店铺和商品范围的报告版本。")
            if data.get("source_investigation"):
                investigation = get_object_or_404(
                    scoped(InvestigationRun.objects.all(), request.user), pk=data["source_investigation"]
                )
                if (investigation.shop_id, investigation.child_asin, investigation.product_id) != (
                    shop.pk, data["child_asin"], data.get("product")
                ):
                    raise serializers.ValidationError("动作必须绑定同一店铺和商品范围的调查版本。")
                report_versions = report.input_versions
                investigation_versions = investigation.input_versions
                if (
                    not report.metric_snapshot_id
                    or str(report.metric_snapshot_id) != investigation_versions.get("metric_snapshot")
                    or report_versions.get("source_manifest") != investigation_versions.get("source_manifest")
                    or report_versions.get("mapping_versions", {}) != investigation_versions.get("mapping_versions", {})
                    or not investigation_versions.get("mapping_versions")
                ):
                    raise serializers.ValidationError("报告与调查的快照、来源清单或 mapping 版本不一致或缺失。")
                if investigation.status not in {"completed", "partial"} or not investigation_versions.get("source_manifest"):
                    raise serializers.ValidationError("只能确认已完成且具有冻结来源版本的调查。")
                required_refs = {
                    f"report:{report.pk}",
                    f"investigation:{investigation.pk}",
                    f"snapshot:{report.metric_snapshot_id}",
                }
                if not required_refs.issubset(set(data.get("evidence_refs", []))):
                    raise serializers.ValidationError("来源闭环任务必须引用所绑定的报告、调查和快照编号。")
        if data.get("event"):
            event = get_object_or_404(scoped(BusinessEvent.objects.all(), request.user), pk=data["event"])
            if (event.shop_id, event.child_asin, event.product_id) != (
                shop.pk,
                data["child_asin"],
                data.get("product"),
            ):
                raise serializers.ValidationError("事件与任务的店铺、ASIN 和商品范围必须一致。")
        if data.get("assigned_to"):
            assignee = get_object_or_404(
                get_user_model().objects.filter(is_active=True), pk=data["assigned_to"]
            )
            require_role(assignee, shop, "operator", "quality", "support", "data_admin")
            authorize_object(assignee, shop, data["child_asin"], data.get("product"))
        task = ActionTask.objects.create(
            shop=shop,
            event_id=data.get("event"),
            source_report_id=data.get("source_report"),
            source_investigation_id=data.get("source_investigation"),
            human_confirmed_by=request.user if data.get("confirm_source") else None,
            human_confirmed_at=timezone.now() if data.get("confirm_source") else None,
            human_confirmation_note=data.get("confirmation_note", "") if data.get("confirm_source") else "",
            child_asin=data["child_asin"],
            product_id=data.get("product"),
            title=data["title"],
            assigned_to_id=data.get("assigned_to"),
            created_by=request.user,
            due_at=data.get("due_at"),
            **{field: data[field] for field in ("action_type", "question", "completion_criteria", "evidence_refs", "observed_metrics", "is_formal") if field in data},
        )
        if task.assigned_to_id:
            Notification.objects.create(user=task.assigned_to, task=task, kind="assigned", data_version=f"created-{task.pk}")
        audit(
            request.user,
            shop,
            "action_task.created",
            task.pk,
            after={"title": task.title},
            request_id=request.request_id,
        )
        return Response(self.get_serializer(task).data, status=201)

    @extend_schema(request=DraftRevisionCommand, responses=ActionTaskSerializer)
    @action(detail=True, methods=["post"], url_path="revise-draft")
    def revise_draft(self, request, pk=None):
        command = DraftRevisionCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            task = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            require_role(request.user, task.shop, "operator", "quality", "support", "data_admin")
            if task.version != data["expected_version"]:
                raise Conflict("任务草稿已被修改，请刷新后重试。")
            if task.status != "open" or task.is_formal or task.executions.exists():
                raise Conflict("只有尚未正式指派或执行的开放草稿可以修订。")
            before = {field: getattr(task, field) for field in data if field != "expected_version"}
            fields = []
            for field, value in data.items():
                if field == "expected_version":
                    continue
                setattr(task, field, value)
                fields.append(field)
            task.version += 1
            task.save(update_fields=[*fields, "version"])
            audit(request.user, task.shop, "action_task.draft_revised", task.pk,
                  before=before, after={field: getattr(task, field) for field in fields}, request_id=request.request_id)
        return Response(self.get_serializer(task).data)

    @extend_schema(request=ExecutionCommand, responses=ActionTaskSerializer)
    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        command = ExecutionCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            task = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            require_role(request.user, task.shop, "operator", "quality", "support", "data_admin")
            if task.version != data["expected_version"] or task.status == "completed":
                raise Conflict("待核查任务已被修改，请刷新后重试。")
            effective = data.get("effective_at")
            review_at = data.get("review_at")
            metrics = data.get("observed_metrics", task.observed_metrics)
            evidence_refs = data.get("evidence_refs", [])
            validate_evidence_refs(evidence_refs)
            if effective and effective > timezone.now():
                raise serializers.ValidationError("实际生效时间不能在未来。")
            if review_at and (not effective or review_at <= effective):
                raise serializers.ValidationError("复查时间必须晚于实际生效时间。")
            if task.is_formal and not (effective and review_at and metrics and evidence_refs):
                raise serializers.ValidationError("正式动作完成需记录依据、生效时间、观察指标和复查时间。")
            task.effective_at, task.observed_metrics = effective, metrics
            if review_at:
                task.review_at, task.review_status = review_at, "scheduled"
            ActionExecution.objects.create(
                task=task,
                actor=request.user,
                notes=data["notes"],
                evidence_refs=evidence_refs,
                completed_at=timezone.now(),
            )
            task.status, task.version = "completed", task.version + 1
            task.save(update_fields=["status", "version", "effective_at", "observed_metrics", "review_at", "review_status"])
            audit(
                request.user,
                task.shop,
                "action_task.completed",
                task.pk,
            after={"notes": data["notes"], "evidence_refs": evidence_refs},
                request_id=request.request_id,
            )
        return Response(self.get_serializer(task).data)

    @extend_schema(request=ReviewCommand, responses=ActionTaskSerializer)
    @action(detail=True, methods=["post"], url_path="review")
    def review(self, request, pk=None):
        command = ReviewCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            task = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            require_role(request.user, task.shop, "operator", "quality", "support", "data_admin")
            if task.version != data["expected_version"]:
                raise Conflict("复查任务已被修改，请刷新后重试。")
            if data["status"] == "completed":
                from insight.action_review import validate_review
                comparison = validate_review(task, data, request.user)
            else:
                comparison = None
            if data["status"] == "scheduled" and not data.get("review_at"):
                raise serializers.ValidationError("安排复查必须填写日期。")
            if data["status"] == "scheduled" and data.get("review_at") <= timezone.now():
                raise serializers.ValidationError("安排复查的时间必须在当前时间之后。")
            task.review_at, task.review_status, task.review_result = (
                data.get("review_at"),
                data["status"],
                data.get("result", ""),
            )
            task.review_comparison = comparison or {}
            task.version += 1
            task.save(update_fields=["review_at", "review_status", "review_result", "review_comparison", "version"])
            audit(
                request.user,
                task.shop,
                "action_task.reviewed",
                task.pk,
                after={
                    "review_at": str(task.review_at) if task.review_at else None,
                    "status": task.review_status,
                    "snapshot": str(data["snapshot"]) if data.get("snapshot") else None,
                    "result": task.review_result,
                    "comparison": comparison,
                },
                request_id=request.request_id,
            )
        return Response(self.get_serializer(task).data)


class ChangeProposalSerializer(serializers.ModelSerializer):
    execution_policy = serializers.SerializerMethodField()
    manual_authorization = serializers.SerializerMethodField()

    def get_execution_policy(self, obj) -> str:
        return policy_description(obj.operation)

    def get_manual_authorization(self, obj) -> dict[str, str] | None:
        return (obj.execution_result or {}).get("manual_authorization")

    class Meta:
        model = ChangeProposal
        fields = [
            "id",
            "shop",
            "child_asin",
            "product",
            "operation",
            "target",
            "before",
            "after",
            "evidence_refs",
            "status",
            "requested_by",
            "reviewed_by",
            "review_note",
            "version",
            "idempotency_key",
            "payload_hash", "approved_payload_hash", "expires_at", "execution_result", "executed_at",
            "execution_policy",
            "manual_authorization",
            "created_at",
        ]
        read_only_fields = ["id", "requested_by", "reviewed_by", "status", "version", "created_at"]


class ProposalCommand(serializers.Serializer):
    shop = serializers.UUIDField()
    child_asin = serializers.RegexField(r"^[A-Z0-9]{10}$")
    product = serializers.UUIDField(required=False, allow_null=True)
    operation = serializers.ChoiceField(
        choices=["internal_followup", "listing_change", "price_change", "inventory_change"]
    )
    target = serializers.JSONField()
    before = serializers.JSONField(required=False)
    after = serializers.JSONField(required=False)
    evidence_refs = serializers.ListField(child=serializers.CharField(max_length=200), required=False)
    idempotency_key = serializers.CharField(max_length=150)

    def validate(self, data):
        validate_evidence_refs(data.get("evidence_refs", []))
        if data["operation"] in {"listing_change", "price_change", "inventory_change"}:
            if not isinstance(data.get("target"), dict) or not data["target"]:
                raise serializers.ValidationError("外部经营变更提案必须填写结构化目标，且只能人工执行。")
        return data


class ProposalReviewCommand(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approve", "reject"])
    note = serializers.CharField(min_length=1, max_length=1000)
    expected_version = serializers.IntegerField(min_value=1)
    payload_hash = serializers.CharField(max_length=64, required=False)


class ProposalExecuteCommand(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)
    payload_hash = serializers.CharField(min_length=64, max_length=64)


class ManualAuthorizationCommand(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)
    note = serializers.CharField(min_length=1, max_length=1000)
    handoff_ref = serializers.RegexField(r"^[A-Za-z0-9._-]{1,150}$")


class ChangeProposalViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ChangeProposalSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ChangeProposal.objects.none()
        qs = scoped(ChangeProposal.objects.all(), self.request.user)
        if shop := self.request.query_params.get("shop"):
            qs = qs.filter(shop_id=shop)
        # Newly submitted proposals must be visible immediately, regardless of
        # their status. Sorting by status first can hide a fresh pending
        # proposal behind pages of already completed/manual hand-offs.
        return qs.order_by("-created_at", "-id")

    @transaction.atomic
    @extend_schema(request=ProposalCommand, responses={201: ChangeProposalSerializer})
    def create(self, request):
        command = ProposalCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        shop = get_object_or_404(shops_for(request.user), pk=data["shop"])
        require_role(request.user, shop, "operator", "quality", "support", "data_admin")
        authorize_object(request.user, shop, data["child_asin"], data.get("product"))
        proposal, created = ChangeProposal.objects.get_or_create(
            shop=shop,
            requested_by=request.user,
            idempotency_key=data["idempotency_key"],
            defaults={
                "child_asin": data["child_asin"],
                "product_id": data.get("product"),
                "operation": data["operation"],
                "target": data["target"],
                "before": data.get("before", {}),
                "after": data.get("after", {}),
                "evidence_refs": data.get("evidence_refs", []),
            },
        )
        expected = {
            "child_asin": data["child_asin"],
            "product_id": data.get("product"),
            "operation": data["operation"],
            "target": data["target"],
            "before": data.get("before", {}),
            "after": data.get("after", {}),
            "evidence_refs": data.get("evidence_refs", []),
        }
        if not created and any(getattr(proposal, key) != value for key, value in expected.items()):
            raise Conflict("同一提案标识不能用于不同变更。")
        if created:
            proposal.expires_at = timezone.now() + timedelta(hours=24)
            if proposal.operation == "internal_followup":
                validate_internal_proposal(proposal, request.user)
            proposal.payload_hash = payload_hash(proposal)
            proposal.save(update_fields=["expires_at", "payload_hash"])
            audit(
                request.user,
                shop,
                "change_proposal.created",
                proposal.pk,
                after={"operation": proposal.operation},
                request_id=request.request_id,
            )
        return Response(self.get_serializer(proposal).data, status=201 if created else 200)

    @extend_schema(request=ProposalReviewCommand, responses=ChangeProposalSerializer)
    @action(detail=True, methods=["post"], url_path="review")
    def review(self, request, pk=None):
        command = ProposalReviewCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            proposal = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            require_role(request.user, proposal.shop, "data_admin")
            if proposal.version != data["expected_version"] or proposal.status != "pending":
                raise Conflict("提案已被审核或版本已变化，请刷新后重试。")
            if not proposal.expires_at or proposal.expires_at <= timezone.now():
                raise Conflict("提案已过期，请重新提交。")
            if data.get("payload_hash") != proposal.payload_hash or payload_hash(proposal) != proposal.payload_hash:
                raise Conflict("必须审核当前精确提案内容。")
            if proposal.operation == "internal_followup":
                validate_internal_proposal(proposal, request.user)
            proposal.approved_payload_hash = payload_hash(proposal) if data["decision"] == "approve" else ""
            proposal.status = "approved" if data["decision"] == "approve" else "rejected"
            proposal.reviewed_by, proposal.review_note, proposal.version = (
                request.user,
                data["note"],
                proposal.version + 1,
            )
            proposal.save(update_fields=["status", "reviewed_by", "review_note", "version", "approved_payload_hash"])
            audit(
                request.user,
                proposal.shop,
                f"change_proposal.{proposal.status}",
                proposal.pk,
                after={"operation": proposal.operation, "note": proposal.review_note},
                request_id=request.request_id,
            )
        return Response(self.get_serializer(proposal).data)


    @extend_schema(request=ProposalExecuteCommand, responses=ChangeProposalSerializer)
    @action(detail=True, methods=["post"])
    def execute(self, request, pk=None):
        proposal = self.get_object()
        command = ProposalExecuteCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        result = execute_proposal(request.user, proposal.pk, data["expected_version"], data["payload_hash"], request.request_id)
        return Response(self.get_serializer(result).data)

    @extend_schema(request=ManualAuthorizationCommand, responses=ChangeProposalSerializer)
    @action(detail=True, methods=["post"], url_path="authorize-manual")
    def authorize_manual(self, request, pk=None):
        """Record a human hand-off for an external proposal without calling Amazon."""
        command = ManualAuthorizationCommand(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            proposal = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            require_role(request.user, proposal.shop, "data_admin")
            if proposal.operation not in {"listing_change", "price_change", "inventory_change"}:
                raise serializers.ValidationError("只有价格、库存或 Listing 提案需要人工外部授权。")
            if proposal.status != "approved":
                raise Conflict("只有已批准且仍在有效期内的提案可以记录人工授权。")
            if not proposal.expires_at or proposal.expires_at <= timezone.now():
                raise Conflict("提案已过期，请重新提交并审核。")
            if proposal.version != data["expected_version"]:
                raise Conflict("提案版本已变化，请刷新后重试。")
            if proposal.payload_hash != payload_hash(proposal) or proposal.approved_payload_hash != proposal.payload_hash:
                raise Conflict("批准内容已变化，必须重新审核精确提案。")
            current = (proposal.execution_result or {}).get("manual_authorization")
            if current:
                if current.get("handoff_ref") != data["handoff_ref"]:
                    raise Conflict("提案已经绑定另一份人工交接记录。")
                return Response(self.get_serializer(proposal).data)
            if ChangeProposal.objects.filter(
                execution_result__manual_authorization__handoff_ref=data["handoff_ref"]
            ).exclude(pk=proposal.pk).exists():
                raise Conflict("人工交接编号已用于其他提案，请使用唯一编号。")
            authorization = {
                "status": "authorized",
                "authorized_by": request.user.username,
                "authorized_at": timezone.now().isoformat(),
                "handoff_ref": data["handoff_ref"],
                "note": data["note"],
                "system_execution": "never",
            }
            proposal.execution_result = {
                **(proposal.execution_result or {}),
                "manual_authorization": authorization,
                "external_execution": {
                    "status": "not_executed",
                    "reason": "系统不连接 Amazon 或 ERP 写接口，需由人工按交接编号执行并回填结果。",
                },
            }
            proposal.status = "manual_authorized"
            proposal.version += 1
            proposal.save(update_fields=["execution_result", "status", "version"])
            audit(
                request.user,
                proposal.shop,
                "change_proposal.manual_authorized",
                proposal.pk,
                after=authorization,
                request_id=request.request_id,
            )
        return Response(self.get_serializer(proposal).data)
