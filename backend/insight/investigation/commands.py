"""Transactional internal writes for the investigation tool registry."""
import hashlib
import json

from django.db import transaction
from rest_framework import serializers

from insight.access import require_role
from insight.api_errors import Conflict
from insight.metrics.facts import authorize_object
from insight.models import ActionTask, InvestigationCommandReceipt, InvestigationRun, Shop
from insight.services import audit


class DraftInvestigationTask(serializers.Serializer):
    logical_action_id = serializers.RegexField(r"^[a-zA-Z0-9_.:-]{1,100}$")
    title = serializers.CharField(max_length=240)
    question = serializers.CharField(max_length=3000)
    completion_criteria = serializers.CharField(max_length=3000)
    evidence_refs = serializers.ListField(child=serializers.CharField(max_length=200), min_length=1, max_length=100)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError({"non_field_errors": ["调查草稿不接受额外权限、负责人、截止日期或经营变更字段。"]})
        return super().to_internal_value(data)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@transaction.atomic
def create_draft_task(run, payload):
    from .graph import assert_execution_owner

    command = DraftInvestigationTask(data=payload)
    command.is_valid(raise_exception=True)
    data = command.validated_data
    # Serialize cross-run deduplication for the same shop, not only one run.
    Shop.objects.select_for_update().get(pk=run.shop_id)
    locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
    assert_execution_owner(run, locked)
    require_role(run.requested_by, run.shop, "operator", "quality", "support", "data_admin")
    authorize_object(run.requested_by, run.shop, locked.child_asin, locked.product_id)
    if locked.read_only_refresh:
        raise Conflict("数据修订自动刷新只更新调查产物，不继承历史动作的执行授权。")
    if locked.status != "running":
        raise Conflict("仅当前执行中的调查可以写入内部核查草稿。")
    payload_hash = digest(data)
    prior = InvestigationCommandReceipt.objects.filter(run=run, logical_action_id=data["logical_action_id"]).first()
    if prior:
        if prior.payload_hash != payload_hash:
            raise Conflict("同一调查动作编号不能提交不同内容。")
        return prior.result
    trusted = {str(item["id"]) for item in locked.state.get("evidence", []) if item.get("id") and item.get("status") != "missing"}
    if not set(data["evidence_refs"]) <= trusted:
        raise serializers.ValidationError("任务只能引用本次调查已保存的证据。")
    fingerprint = digest({"shop": str(run.shop_id), "asin": locked.child_asin,
        "product": str(locked.product_id) if locked.product_id else None,
        "question": " ".join(data["question"].split()), "evidence": sorted(set(data["evidence_refs"])),
        "completion_criteria": " ".join(data["completion_criteria"].split())})
    previous = InvestigationCommandReceipt.objects.filter(evidence_fingerprint=fingerprint).select_related("task").first()
    task = previous.task if previous else ActionTask.objects.create(
        shop=run.shop, child_asin=locked.child_asin, product_id=locked.product_id,
        title=data["title"], question=data["question"], completion_criteria=data["completion_criteria"],
        evidence_refs=data["evidence_refs"], action_type="investigation", is_formal=False, created_by=run.requested_by,
    )
    result = {"task_id": str(task.pk), "created": previous is None, "is_formal": task.is_formal, "assigned_to": task.assigned_to_id}
    receipt = InvestigationCommandReceipt.objects.create(run=run, logical_action_id=data["logical_action_id"],
        payload_hash=payload_hash, evidence_fingerprint=fingerprint, task=task, result=result)
    audit(run.requested_by, run.shop, "investigation.task_created" if previous is None else "investigation.task_linked", task.pk,
        after={"run_id": str(run.pk), "receipt_id": str(receipt.pk), "logical_action_id": data["logical_action_id"],
               "evidence_fingerprint": fingerprint, "evidence_refs": data["evidence_refs"]}, request_id=str(run.pk))
    return result
