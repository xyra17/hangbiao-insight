from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from insight.access import is_admin, require_role
from insight.api_errors import Conflict
from insight.metrics.facts import authorize_object
from insight.models import ActionTask, BusinessEvent, Notification
from insight.services import audit


class EventCommand(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)
    operation = serializers.ChoiceField(choices=["claim", "observe", "close", "snooze", "unsnooze"])
    owner = serializers.IntegerField(required=False)
    action_task = serializers.UUIDField(required=False)
    reason = serializers.CharField(max_length=1000)
    closure_reason = serializers.ChoiceField(choices=["resolved", "false_positive", "known_change", "duplicate"], required=False)
    evidence_refs = serializers.ListField(child=serializers.UUIDField(), required=False)
    duplicate_of = serializers.UUIDField(required=False)
    risk_resolved = serializers.BooleanField(default=False)
    snoozed_until = serializers.DateTimeField(required=False)


@transaction.atomic
def change_event(actor, event_id, data, request_id):
    event = get_object_or_404(BusinessEvent.objects.select_for_update(), pk=event_id)
    require_role(actor, event.shop, "operator", "quality", "support", "data_admin")
    authorize_object(actor, event.shop, event.child_asin, event.product_id)
    if event.version != data["expected_version"]:
        raise Conflict("事件已变化，请刷新后核对最新证据。")
    operation = data["operation"]
    if event.status == "closed":
        raise Conflict("已关闭事件不能执行此操作，再次触发时由扫描重新打开。")
    before = {"status": event.status, "version": event.version, "owner": event.owner_id,
              "snoozed_until": str(event.snoozed_until) if event.snoozed_until else None}
    if operation == "claim":
        owner = get_object_or_404(get_user_model().objects.filter(is_active=True), pk=data.get("owner", actor.pk))
        require_role(owner, event.shop, "operator", "quality", "support", "data_admin")
        authorize_object(owner, event.shop, event.child_asin, event.product_id)
        event.owner = owner
        if event.status == "pending":
            event.status = "processing"
        Notification.objects.get_or_create(user=owner, event=event, kind="assigned", data_version=f"command-{event.version + 1}")
    else:
        if event.owner_id != actor.pk and not is_admin(actor, event.shop):
            raise PermissionDenied("请由事件负责人或管理员处理，尚未领取时请先领取。")
        if operation == "observe":
            if event.status != "processing":
                raise Conflict("只有处理中的事件可以进入观察。")
            task = ActionTask.objects.filter(pk=data.get("action_task"), event=event, status="completed").first()
            if not task or not task.executions.exists() or not task.effective_at or not task.observed_metrics or not task.review_at:
                raise serializers.ValidationError("进入观察需关联已完成动作，填写生效时间、观察指标和复查日期。")
            event.status = "observing"
        elif operation == "close":
            reason = data.get("closure_reason")
            refs = [str(item) for item in data.get("evidence_refs", [])]
            known = {str(item) for item in event.evidence.values_list("snapshot_id", flat=True)}
            if not reason or not refs or not set(refs) <= known:
                raise serializers.ValidationError("关闭需选择结论，并引用本事件的指标快照作为核查依据。")
            if reason == "resolved" and not data["risk_resolved"]:
                raise serializers.ValidationError("必须明确确认风险已解决；分派动作不代表风险解除。")
            if reason == "duplicate":
                duplicate = BusinessEvent.objects.filter(pk=data.get("duplicate_of"), shop=event.shop,
                    child_asin=event.child_asin, product_id=event.product_id, family=event.family).exclude(pk=event.pk).first()
                if not duplicate:
                    raise serializers.ValidationError("重复事件需指定同店铺、商品和问题族的另一事件。")
                event.duplicate_of = duplicate
            event.status, event.closed_at = "closed", timezone.now()
            event.closure_reason, event.closure_note, event.closure_evidence = reason, data["reason"], refs
            event.snoozed_until, event.snooze_reason = None, ""
        elif operation == "snooze":
            until = data.get("snoozed_until")
            if not until or until <= timezone.now():
                raise serializers.ValidationError("暂缓截止时间必须晚于当前时间。")
            event.snoozed_until, event.snooze_reason = until, data["reason"]
        elif operation == "unsnooze":
            event.snoozed_until, event.snooze_reason = None, ""
    event.version += 1
    event.save()
    audit(actor, event.shop, f"event.{operation}", event.pk, before=before,
          after={"status": event.status, "version": event.version, "owner": event.owner_id,
                 "reason": data["reason"], "closure_reason": event.closure_reason,
                 "evidence_refs": event.closure_evidence,
                 "snoozed_until": str(event.snoozed_until) if event.snoozed_until else None}, request_id=request_id)
    return event


@transaction.atomic
def expire_snoozes():
    for event in BusinessEvent.objects.select_for_update(skip_locked=True).filter(snoozed_until__lte=timezone.now()):
        event.snoozed_until, event.snooze_reason = None, ""
        event.version += 1
        event.save(update_fields=["snoozed_until", "snooze_reason", "version"])
        audit(None, event.shop, "event.snooze_expired", event.pk, after={"version": event.version}, request_id="snooze-expiry")
