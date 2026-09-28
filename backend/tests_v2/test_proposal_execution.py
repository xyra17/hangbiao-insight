from datetime import timedelta

import pytest
from django.utils import timezone

from insight.models import ActionTask, AuditEntry, ChangeProposal, Notification, ShopGrant

pytestmark = pytest.mark.django_db


@pytest.fixture
def proposal_case(metric_scope, client):
    users, shops = metric_scope
    task = ActionTask.objects.create(shop=shops[0], child_asin="B000TEST01", title="包装核验", question="破损原因", completion_criteria="保留核验记录",
        observed_metrics=["returns"], created_by=users["a"])
    client.force_authenticate(users["a"])
    response = client.post("/api/v1/change-proposals/", {"shop":str(shops[0].pk), "child_asin":task.child_asin,
        "operation":"internal_followup", "target":{"action_task":str(task.pk), "expected_version":1},
        "before":{"assigned_to":None,"due_at":None}, "after":{"assigned_to":users["a"].pk,"due_at":(timezone.now()+timedelta(days=7)).isoformat()},
        "idempotency_key":"assignment-1"}, format="json")
    assert response.status_code == 201, response.data
    return users, task, response.data


def approve(client, users, proposal):
    client.force_authenticate(users["admin"])
    response=client.post(f"/api/v1/change-proposals/{proposal['id']}/review/", {
        "expected_version":1,"payload_hash":proposal["payload_hash"],"decision":"approve","note":"确认负责人和截止日期"},format="json")
    assert response.status_code == 200, response.data
    return response.data


def execute(client, proposal):
    return client.post(f"/api/v1/change-proposals/{proposal['id']}/execute/", {
        "expected_version":proposal["version"],"payload_hash":proposal["approved_payload_hash"]},format="json")


def test_approved_internal_assignment_executes_once_with_audit_and_notice(proposal_case, client):
    users, task, proposal=proposal_case
    approved=approve(client, users, proposal)
    result=execute(client, approved)
    assert result.status_code == 200 and result.data["status"] == "succeeded"
    assert execute(client, approved).status_code == 200
    task.refresh_from_db()
    assert task.is_formal and task.assigned_to_id == users["a"].pk and task.version == 2
    assert Notification.objects.filter(task=task,kind="assigned").count() == 1
    assert AuditEntry.objects.filter(action="change_proposal.executed").count() == 1
    client.force_authenticate(users["a"])
    notices=client.get("/api/v1/notifications/")
    assert notices.data["count"] == 1
    ShopGrant.objects.filter(user=users["a"]).delete()
    assert client.get("/api/v1/notifications/").data["count"] == 0


@pytest.mark.parametrize("mutation", ["payload", "target", "expiry", "owner_permission"])
def test_old_approval_cannot_execute_changed_content_target_or_permissions(proposal_case, client, mutation):
    users, task, proposal=proposal_case
    approved=approve(client, users, proposal)
    if mutation == "payload":
        ChangeProposal.objects.filter(pk=proposal["id"]).update(after={"assigned_to":users["b"].pk})
    elif mutation == "target":
        ActionTask.objects.filter(pk=task.pk).update(version=2)
    elif mutation == "expiry":
        ChangeProposal.objects.filter(pk=proposal["id"]).update(expires_at=timezone.now()-timedelta(seconds=1))
    else:
        ShopGrant.objects.filter(user=users["a"]).delete()
    assert execute(client, approved).status_code in {403,409}
    task.refresh_from_db()
    assert not task.is_formal and task.assigned_to_id is None
    assert not Notification.objects.exists()


def test_internal_execution_audit_failure_rolls_back_all_effects(proposal_case, client, monkeypatch):
    from insight import proposals
    users, task, proposal=proposal_case
    approved=approve(client, users, proposal)
    def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(proposals,"audit",unavailable)
    assert execute(client, approved).status_code == 500
    task.refresh_from_db()
    assert not task.is_formal and task.version == 1
    assert ChangeProposal.objects.get(pk=proposal["id"]).status == "approved"
    assert not Notification.objects.exists()


def test_overdue_review_reminder_is_idempotent_and_scope_checked(proposal_case, client):
    from insight.action_notifications import notify_overdue_reviews
    users, task, _=proposal_case
    task.assigned_to=users["a"]
    task.review_status="scheduled"
    task.review_at=timezone.now()-timedelta(days=1)
    task.save()
    notify_overdue_reviews()
    notify_overdue_reviews()
    assert Notification.objects.filter(task=task,kind="review_overdue").count() == 1
    task.review_at=timezone.now()-timedelta(days=2)
    task.save()
    ShopGrant.objects.filter(user=users["a"]).delete()
    notify_overdue_reviews()
    assert Notification.objects.filter(task=task).count() == 1


def test_pending_and_approved_proposals_expire_to_terminal_state(metric_scope, client):
    from insight.proposals import expire_proposals

    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/change-proposals/",
        {
            "shop": str(shops[0].pk),
            "child_asin": "B000TEST01",
            "operation": "price_change",
            "target": {"field": "price"},
            "before": {"value": "20"},
            "after": {"value": "19"},
            "evidence_refs": ["snapshot:expiry"],
            "idempotency_key": "expiry-pending-1",
        },
        format="json",
    )
    assert response.status_code == 201
    ChangeProposal.objects.filter(pk=response.data["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert len(expire_proposals()) == 1
    expired = ChangeProposal.objects.get(pk=response.data["id"])
    assert expired.status == "expired"
    assert expired.execution_result["reason"]
    client.force_authenticate(users["admin"])
    blocked = client.post(
        f"/api/v1/change-proposals/{expired.pk}/review/",
        {"decision": "approve", "note": "过期不能批准", "expected_version": expired.version, "payload_hash": expired.payload_hash},
        format="json",
    )
    assert blocked.status_code == 409
