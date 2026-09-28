import pytest
from rest_framework.exceptions import PermissionDenied, ValidationError

from insight.api_errors import Conflict
from insight.investigation.commands import create_draft_task
from insight.models import ActionTask, AuditEntry, InvestigationCommandReceipt, InvestigationRun, ShopGrant

pytestmark = pytest.mark.django_db


@pytest.fixture
def write_case(metric_scope):
    users, shops = metric_scope
    run = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01",
        query="调查退货", status="running", state={"evidence":[{"id":"frozen-evidence", "type":"metric_snapshot"}]})
    payload = {"logical_action_id":"check-1", "title":"核查包装", "question":"包装是否存在破损？", "completion_criteria":"记录包装核验结论",
        "evidence_refs":["frozen-evidence"]}
    return run, payload


def test_internal_write_replay_and_cross_run_dedup(write_case):
    run, data = write_case
    first = create_draft_task(run, data)
    assert create_draft_task(run, data) == first
    another = InvestigationRun.objects.create(shop=run.shop, requested_by=run.requested_by,
        child_asin=run.child_asin, query=run.query, status="running", state=run.state)
    reused = create_draft_task(another, data)
    assert reused["task_id"] == first["task_id"] and not reused["created"]
    assert ActionTask.objects.count() == 1 and InvestigationCommandReceipt.objects.count() == 2
    task = ActionTask.objects.get()
    assert task.assigned_to is None and task.due_at is None and not task.is_formal
    assert AuditEntry.objects.filter(action__startswith="investigation.task_").count() == 2
    with pytest.raises(Conflict):
        create_draft_task(run, {**data, "title":"不同动作"})


def test_internal_write_scope_extra_fields_evidence_and_cancel(write_case):
    run, data = write_case
    with pytest.raises(ValidationError):
        create_draft_task(run, {**data, "assigned_to":run.requested_by_id})
    with pytest.raises(ValidationError):
        create_draft_task(run, {**data, "evidence_refs":["foreign-evidence"]})
    InvestigationRun.objects.filter(pk=run.pk).update(status="cancelled")
    with pytest.raises(Conflict):
        create_draft_task(run, data)
    InvestigationRun.objects.filter(pk=run.pk).update(status="running")
    ShopGrant.objects.filter(user=run.requested_by, shop=run.shop).delete()
    with pytest.raises(PermissionDenied):
        create_draft_task(run, data)
    assert not ActionTask.objects.exists()


def test_internal_write_audit_failure_rolls_back_receipt_and_task(write_case, monkeypatch):
    from insight.investigation import commands
    run, data = write_case
    def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(commands, "audit", unavailable)
    with pytest.raises(RuntimeError):
        create_draft_task(run, data)
    assert not ActionTask.objects.exists() and not InvestigationCommandReceipt.objects.exists()


def test_internal_write_old_execution_generation_rejected(write_case):
    run, data = write_case
    InvestigationRun.objects.filter(pk=run.pk).update(lease_token=1)
    with pytest.raises(RuntimeError, match="lease_superseded"):
        create_draft_task(run, data)
    assert not ActionTask.objects.exists()
