from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from insight.access import require_role
from insight.metrics.facts import authorize_object
from insight.models import ActionTask, Notification
from insight.services import audit


@transaction.atomic
def notify_overdue_reviews():
    tasks = ActionTask.objects.select_for_update(skip_locked=True).filter(
        review_status="scheduled", review_at__lt=timezone.now(), assigned_to__isnull=False,
    ).select_related("assigned_to", "shop")
    for task in tasks:
        try:
            require_role(task.assigned_to, task.shop, "operator", "quality", "support", "data_admin")
            authorize_object(task.assigned_to, task.shop, task.child_asin, task.product_id)
        except PermissionDenied:
            continue
        notice, created = Notification.objects.get_or_create(user=task.assigned_to, task=task,
            kind="review_overdue", data_version=str(task.review_at))
        if created:
            audit(None, task.shop, "action_task.review_overdue", task.pk,
                after={"notification_id":str(notice.pk), "review_at":str(task.review_at)}, request_id="review-overdue")
