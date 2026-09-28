from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers

from insight.access import require_role
from insight.api_errors import Conflict
from insight.ingestion.contracts import digest
from insight.metrics.facts import authorize_object
from insight.models import ActionTask, ChangeProposal, Notification
from insight.services import audit

EXECUTABLE_INTERNAL_OPERATIONS = frozenset({"internal_followup"})
MANUAL_ONLY_OPERATIONS = frozenset({"listing_change", "price_change", "inventory_change"})


def action_policy(operation):
    """Return the only operation class that may be executed automatically inside the app."""
    return "internal_task_assignment" if operation in EXECUTABLE_INTERNAL_OPERATIONS else "manual_only"


def policy_description(operation):
    if operation in EXECUTABLE_INTERNAL_OPERATIONS:
        return "internal_task_assignment"
    if operation in MANUAL_ONLY_OPERATIONS:
        return "manual_external_authorization_required"
    return "unsupported_manual_review"


def expire_proposals(now=None):
    """Persist the terminal ``expired`` state for proposals past their deadline."""
    now = now or timezone.now()
    expired = []
    with transaction.atomic():
        for proposal in ChangeProposal.objects.select_for_update().filter(
            status__in=["pending", "approved", "manual_authorized"], expires_at__isnull=False, expires_at__lte=now
        ):
            proposal.status = "expired"
            proposal.version += 1
            proposal.execution_result = {"reason": "提案已过期，不能继续审核或执行。"}
            proposal.save(update_fields=["status", "version", "execution_result"])
            audit(
                None,
                proposal.shop,
                "change_proposal.expired",
                proposal.pk,
                after={"status": proposal.status, "expires_at": str(proposal.expires_at)},
                request_id="proposal-expiry",
            )
            expired.append(proposal.pk)
    return expired


def payload_hash(proposal):
    return digest({"shop":str(proposal.shop_id), "asin":proposal.child_asin, "product":str(proposal.product_id),
        "operation":proposal.operation, "target":proposal.target, "before":proposal.before, "after":proposal.after,
        "evidence_refs":proposal.evidence_refs, "expires_at":proposal.expires_at})


class Assignment(serializers.Serializer):
    assigned_to = serializers.IntegerField()
    due_at = serializers.DateTimeField()

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) != {"assigned_to", "due_at"}:
            raise serializers.ValidationError({"after":"内部指派只允许负责人和截止日期两个字段。"})
        return super().to_internal_value(data)


def validate_internal_proposal(proposal, actor, *, lock=False):
    if not isinstance(proposal.target, dict) or set(proposal.target) != {"action_task", "expected_version"}:
        raise serializers.ValidationError("内部提案目标必须明确为 action_task 和 expected_version。")
    try:
        from uuid import UUID
        target_id = UUID(str(proposal.target["action_task"]))
    except (ValueError, TypeError, AttributeError):
        raise serializers.ValidationError("目标任务编号无效。") from None
    qs = ActionTask.objects.select_for_update() if lock else ActionTask.objects.all()
    task = get_object_or_404(qs, pk=target_id, shop=proposal.shop,
        child_asin=proposal.child_asin, product_id=proposal.product_id)
    if task.version != proposal.target["expected_version"] or task.status == "completed":
        raise Conflict("目标任务已变更或完成，原批准不能用于新版本。")
    expected = {"assigned_to":task.assigned_to_id, "due_at":task.due_at.isoformat() if task.due_at else None}
    if proposal.before != expected:
        raise Conflict("提案中的变更前值与当前任务不一致。")
    command = Assignment(data=proposal.after)
    command.is_valid(raise_exception=True)
    data = command.validated_data
    if data["due_at"] <= timezone.now():
        raise serializers.ValidationError("正式指派截止时间必须在未来。")
    if not task.question or not task.completion_criteria or not task.observed_metrics:
        raise serializers.ValidationError("正式指派前须补全任务的核查问题、完成标准和观察指标。")
    owner = get_object_or_404(get_user_model().objects.filter(is_active=True), pk=data["assigned_to"])
    for user in (actor, owner):
        require_role(user, proposal.shop, "operator", "quality", "support", "data_admin")
        authorize_object(user, proposal.shop, task.child_asin, task.product_id)
    return task, owner, data["due_at"]


@transaction.atomic
def execute_proposal(actor, proposal_id, expected_version, expected_hash, request_id):
    proposal = get_object_or_404(ChangeProposal.objects.select_for_update(), pk=proposal_id)
    require_role(actor, proposal.shop, "data_admin")
    authorize_object(actor, proposal.shop, proposal.child_asin, proposal.product_id)
    if action_policy(proposal.operation) != "internal_task_assignment":
        raise serializers.ValidationError("本系统不执行 Amazon、价格、库存或其他外部经营变更。")
    if not expected_hash or expected_hash != proposal.approved_payload_hash or payload_hash(proposal) != expected_hash:
        raise Conflict("批准内容已变更，必须重新审核精确提案。")
    if proposal.status == "succeeded":
        return proposal
    if proposal.version != expected_version or proposal.status != "approved":
        raise Conflict("仅当前已批准版本可以执行。")
    if not proposal.expires_at or proposal.expires_at <= timezone.now():
        raise Conflict("提案已过有效期，须重新提交。")
    # Approval is not a permanent capability after the approver is revoked.
    require_role(proposal.reviewed_by, proposal.shop, "data_admin")
    authorize_object(proposal.reviewed_by, proposal.shop, proposal.child_asin, proposal.product_id)
    task, owner, due_at = validate_internal_proposal(proposal, actor, lock=True)
    task.assigned_to, task.due_at, task.is_formal = owner, due_at, True
    task.version += 1
    task.save(update_fields=["assigned_to", "due_at", "is_formal", "version"])
    Notification.objects.get_or_create(user=owner, task=task, kind="assigned", data_version=f"proposal-{proposal.pk}")
    proposal.status, proposal.executed_at = "succeeded", timezone.now()
    proposal.execution_result = {"task_id":str(task.pk), "task_version":task.version}
    proposal.version += 1
    proposal.save(update_fields=["status", "executed_at", "execution_result", "version"])
    audit(actor, proposal.shop, "change_proposal.executed", proposal.pk,
        before=proposal.before, after={"payload_hash":expected_hash, "task":proposal.execution_result, "changes":proposal.after}, request_id=request_id)
    return proposal
