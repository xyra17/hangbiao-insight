"""Human incident grouping never erases independent signals or resolves risk."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from insight.access import require_role
from insight.api_errors import Conflict
from insight.ingestion.contracts import digest
from insight.models import ActionTask, BusinessEvent, EventGroup, EventGroupingReceipt, Shop
from insight.services import audit

from .facts import authorize_object


class GroupCommand(serializers.Serializer):
    operation = serializers.ChoiceField(choices=["merge", "split"])
    events = serializers.DictField(child=serializers.IntegerField(min_value=1))
    title = serializers.CharField(max_length=240, required=False)
    reason = serializers.CharField(min_length=3, max_length=1000)
    idempotency_key = serializers.CharField(max_length=150)

    def validate_events(self, values):
        from uuid import UUID
        if not 1 <= len(values) <= 100:
            raise serializers.ValidationError("每次请选择1至100个事件。")
        try:
            return {str(UUID(key)): value for key, value in values.items()}
        except ValueError:
            raise serializers.ValidationError("事件编号格式无效。")


@transaction.atomic
def group_events(actor, anchor_id, data, request_id):
    anchor = get_object_or_404(BusinessEvent, pk=anchor_id)
    Shop.objects.select_for_update().get(pk=anchor.shop_id)
    require_role(actor, anchor.shop, "operator", "quality", "support", "data_admin")
    authorize_object(actor, anchor.shop, anchor.child_asin, anchor.product_id)
    fingerprint = digest({"anchor": str(anchor.pk), **data})
    prior = EventGroupingReceipt.objects.filter(shop=anchor.shop, actor=actor, idempotency_key=data["idempotency_key"]).first()
    if prior:
        if prior.payload_hash != fingerprint:
            raise Conflict("同一请求编号不能用于不同合并或拆分。")
        return prior.result
    if str(anchor.pk) not in data["events"]:
        raise ValidationError("请选择当前事件。")
    events = list(BusinessEvent.objects.select_for_update().filter(pk__in=data["events"], shop=anchor.shop, child_asin=anchor.child_asin, product_id=anchor.product_id).order_by("pk"))
    if len(events) != len(data["events"]):
        raise ValidationError("只能合并或拆分同一店铺、ASIN及SKU范围内可访问的事件。")
    for event in events:
        if event.version != data["events"][str(event.pk)]:
            raise Conflict("事件证据或处理状态已变化，请刷新后重新核对。")
    groups = {event.group_id for event in events if event.group_id}
    if data["operation"] == "merge":
        if len(events) < 2 or not data.get("title"):
            raise ValidationError("合并至少需要两个事件及问题组名称。")
        # Require explicit selection of every existing member: no hidden expansion.
        if BusinessEvent.objects.filter(group_id__in=groups).exclude(pk__in=data["events"]).exists():
            raise Conflict("所选事件已属于问题组，请明确选择该组全部成员，或先拆分。")
        group = EventGroup.objects.create(shop=anchor.shop, child_asin=anchor.child_asin, product_id=anchor.product_id, title=data["title"], created_by=actor)
    else:
        if len(groups) != 1 or any(event.group_id is None for event in events):
            raise ValidationError("拆分事件必须属于同一个问题组。")
        group = EventGroup.objects.create(shop=anchor.shop, child_asin=anchor.child_asin, product_id=anchor.product_id, title=data.get("title") or "拆分后的问题组", created_by=actor) if len(events) > 1 else None
    ownership = {}
    for event in events:
        tasks = list(ActionTask.objects.filter(event=event).values("id", "source_report_id"))
        ownership[str(event.pk)] = {
            "evidence": [str(pk) for pk in event.evidence.values_list("pk", flat=True)],
            "tasks": [str(task["id"]) for task in tasks],
            "reports": sorted({str(task["source_report_id"]) for task in tasks if task["source_report_id"]}),
        }
    before = {str(e.pk): str(e.group_id) if e.group_id else None for e in events}
    for event in events:
        event.group = group
        event.version += 1
        event.save(update_fields=["group", "version"])
    for group_id in groups:
        old = EventGroup.objects.select_for_update().get(pk=group_id)
        old.version += 1
        old.save(update_fields=["version"])
    result = {"group_id": str(group.pk) if group else None, "events": {str(e.pk): e.version for e in events}, "operation": data["operation"], "ownership": ownership}
    EventGroupingReceipt.objects.create(shop=anchor.shop, actor=actor, idempotency_key=data["idempotency_key"], payload_hash=fingerprint, result=result)
    audit(actor, anchor.shop, f"event.{data['operation']}", anchor.pk, before=before, after={**result, "reason": data["reason"], "title": data.get("title", "")}, request_id=request_id)
    return result


class GroupResult(serializers.Serializer):
    ownership = serializers.DictField()
    group_id = serializers.UUIDField(allow_null=True)
    events = serializers.DictField(child=serializers.IntegerField())
    operation = serializers.ChoiceField(choices=["merge", "split"])
