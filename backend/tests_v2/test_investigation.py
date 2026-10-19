import pytest

from insight.models import (
    ActionTask,
    InvestigationCommandReceipt,
    InvestigationRun,
    InvestigationStep,
    WorkspaceExecutionLease,
)
from insight.tasks import run_job

pytestmark = pytest.mark.django_db(transaction=True)


def test_langgraph_investigation_is_authorized_bounded_and_traceable(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shops[0].pk),
            "query": "请调查退货评论异常 B000TEST01 的原因",
            "max_steps": 6,
            "idempotency_key": "investigation-1",
        },
        format="json",
    )
    assert response.status_code == 202
    run_job(response.data["id"])
    job = client.get(f"/api/v1/jobs/{response.data['id']}/").data
    assert job["status"] == "completed", job
    run = InvestigationRun.objects.get(pk=job["result"]["investigation_id"])
    assert run.status == "completed"
    assert run.steps_used <= run.max_steps
    assert [step.node for step in InvestigationStep.objects.filter(run=run)] == [
        "locate",
        "choose",
        "execute",
        "execute",
        "execute",
        "compose",
    ]
    assert run.result["asin"] == "B000TEST01"
    assert run.result["limitations"]
    repeat = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shops[0].pk),
            "query": "请调查退货评论异常 B000TEST01 的原因",
            "max_steps": 6,
            "idempotency_key": "investigation-1",
        },
        format="json",
    )
    assert repeat.status_code == 202 and repeat.data["id"] == response.data["id"]


def test_langgraph_stops_at_budget_and_does_not_call_external_tools(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["admin"])
    response = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shops[0].pk),
            "query": "分析 B000TEST01 评论",
            "max_steps": 3,
            "idempotency_key": "investigation-budget",
        },
        format="json",
    )
    assert response.status_code == 202
    run_job(response.data["id"])
    run = InvestigationRun.objects.get(
        pk=client.get(f"/api/v1/jobs/{response.data['id']}/").data["result"]["investigation_id"]
    )
    assert run.steps_used == 3
    assert run.status == "partial"
    assert run.stop_reason == "investigation_budget_exhausted"


def test_investigation_can_be_cancelled_before_worker_and_is_not_restarted(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shops[0].pk),
            "query": "分析 B000TEST01 的退货异常",
            "idempotency_key": "investigation-cancel",
        },
        format="json",
    )
    job_id = response.data["id"]
    created = InvestigationRun.objects.get(query="分析 B000TEST01 的退货异常")
    cancelled = client.post(f"/api/v1/investigations/{created.pk}/cancel/", {}, format="json")
    assert cancelled.status_code == 200 and cancelled.data["status"] == "cancelled"
    run_job(job_id)
    assert InvestigationRun.objects.get(pk=created.pk).status == "cancelled"
    assert WorkspaceExecutionLease.objects.get(job_id=job_id).status == "released"


def test_running_investigation_cancellation_records_cancelled_job_and_releases_slot(
    metric_scope, client, monkeypatch
):
    from insight.investigation import graph
    from insight.models import Job

    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shops[0].pk),
            "query": "分析 B000TEST01 评论并在执行中取消",
            "idempotency_key": "investigation-cancel-running",
        },
        format="json",
    )
    job_id = response.data["id"]
    run = InvestigationRun.objects.get(query="分析 B000TEST01 评论并在执行中取消")
    original = graph.execute_investigation

    def cancel_while_running(current_run):
        job = Job.objects.get(pk=job_id)
        assert job.status == "running"
        cancelled = client.post(f"/api/v1/investigations/{current_run.pk}/cancel/", {}, format="json")
        assert cancelled.status_code == 200 and cancelled.data["status"] == "cancelled"
        return original(current_run)

    monkeypatch.setattr(graph, "execute_investigation", cancel_while_running)
    run_job(job_id)

    run.refresh_from_db()
    job = Job.objects.get(pk=job_id)
    lease = WorkspaceExecutionLease.objects.get(job=job)
    assert run.status == "cancelled"
    assert job.status == "cancelled"
    assert job.result["status"] == "cancelled"
    assert job.finished_at is not None
    assert lease.status == "released"


def test_cancellation_after_persisted_graph_node_stops_later_writes(metric_scope, client, monkeypatch):
    from insight.investigation import graph
    from insight.models import Job

    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shops[0].pk),
            "query": "分析 B000TEST01 评论并在首个节点后取消",
            "idempotency_key": "investigation-cancel-after-node",
        },
        format="json",
    )
    job_id = response.data["id"]
    run = InvestigationRun.objects.get(query="分析 B000TEST01 评论并在首个节点后取消")
    original = graph._persist
    cancellation = {}

    def cancel_after_first_commit(current_run, node, state, output, tool=""):
        result = original(current_run, node, state, output, tool)
        if node == "locate" and not cancellation:
            persisted = InvestigationRun.objects.get(pk=current_run.pk)
            assert persisted.status == "running" and persisted.steps_used == 1
            response = client.post(f"/api/v1/investigations/{current_run.pk}/cancel/", {}, format="json")
            cancellation["response"] = response
        return result

    monkeypatch.setattr(graph, "_persist", cancel_after_first_commit)
    run_job(job_id)

    run.refresh_from_db()
    job = Job.objects.get(pk=job_id)
    lease = WorkspaceExecutionLease.objects.get(job=job)
    assert cancellation["response"].status_code == 200
    assert run.status == "cancelled" and run.steps_used == 1
    assert list(run.steps.values_list("node", flat=True)) == ["locate"]
    assert job.status == "cancelled" and job.result["status"] == "cancelled"
    assert lease.status == "released"


def test_checkpoint_resume_keeps_completed_nodes_and_budget(metric_scope, client, monkeypatch):
    from insight.investigation import graph

    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shops[0].pk),
            "query": "分析 B000TEST01 评论",
            "max_steps": 6,
            "idempotency_key": "checkpoint-resume",
        },
        format="json",
    )
    original = graph._persist

    def interrupted(run, node, state, output, tool=""):
        if node == "choose":
            raise RuntimeError("worker_interrupted")
        return original(run, node, state, output, tool)

    monkeypatch.setattr(graph, "_persist", interrupted)
    run_job(response.data["id"])
    run = InvestigationRun.objects.get(query="分析 B000TEST01 评论")
    assert run.status == "partial" and run.steps_used == 1
    assert WorkspaceExecutionLease.objects.get(job_id=response.data["id"]).status == "released"
    monkeypatch.setattr(graph, "_persist", original)
    resumed = client.post(f"/api/v1/investigations/{run.pk}/resume/", {}, format="json")
    assert resumed.status_code == 202
    run_job(resumed.data["id"])
    run.refresh_from_db()
    from insight.models import Job
    resume_job = Job.objects.get(pk=resumed.data["id"])
    assert run.status == "completed", (run.status, run.stop_reason, run.steps_used, run.state, resume_job.status, resume_job.error, resume_job.result)
    assert run.steps_used == 6
    assert run.steps.filter(node="locate").count() == 1
    assert run.max_steps == 6
    assert WorkspaceExecutionLease.objects.get(job=resume_job).status == "released"


def test_worker_process_interruption_reclaims_expired_execution_lease(metric_scope, client, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    from insight.investigation import graph
    from insight.models import Job

    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shops[0].pk),
            "query": "分析 B000TEST01 评论",
            "max_steps": 6,
            "idempotency_key": "execution-lease-process-interruption",
        },
        format="json",
    )
    job = Job.objects.get(pk=response.data["id"])
    original = graph.execute_investigation

    def process_exit(_run):
        raise SystemExit("simulated worker process death")

    monkeypatch.setattr(graph, "execute_investigation", process_exit)
    with pytest.raises(SystemExit, match="simulated worker process death"):
        run_job(str(job.pk))
    job.refresh_from_db()
    lease = WorkspaceExecutionLease.objects.get(job=job)
    assert job.status == "running" and lease.status == "active"

    stale = timezone.now() - timedelta(minutes=2)
    expired = timezone.now() - timedelta(seconds=1)
    Job.objects.filter(pk=job.pk).update(heartbeat_at=stale)
    WorkspaceExecutionLease.objects.filter(pk=lease.pk).update(
        heartbeat_at=stale, lease_expires_at=expired
    )
    monkeypatch.setattr(graph, "execute_investigation", original)
    run_job(str(job.pk))

    job.refresh_from_db()
    lease.refresh_from_db()
    assert job.status == "completed"
    assert job.attempts == 2
    assert lease.status == "released"


def test_investigation_write_tool_creates_one_draft_and_replays_after_worker_crash(metric_scope, client, monkeypatch):
    from insight.investigation import graph

    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shops[0].pk),
            "query": "请调查 B000TEST01 的评论异常并创建核查任务",
            "max_steps": 8,
            "idempotency_key": "investigation-write-1",
        },
        format="json",
    )
    assert response.status_code == 202
    original = graph._persist
    crashed = {"done": False}

    def interrupt_after_write(run, node, state, output, tool=""):
        result = original(run, node, state, output, tool)
        if node == "write_task" and not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("worker_interrupted_after_internal_write")
        return result

    monkeypatch.setattr(graph, "_persist", interrupt_after_write)
    run_job(response.data["id"])
    run = InvestigationRun.objects.get(query="请调查 B000TEST01 的评论异常并创建核查任务")
    assert run.status == "partial"
    assert ActionTask.objects.filter(created_by=users["a"], child_asin="B000TEST01").count() == 1
    assert InvestigationCommandReceipt.objects.filter(run=run).count() == 1

    monkeypatch.setattr(graph, "_persist", original)
    resumed = client.post(f"/api/v1/investigations/{run.pk}/resume/", {}, format="json")
    assert resumed.status_code == 202
    run_job(resumed.data["id"])
    run.refresh_from_db()
    from insight.models import Job
    resume_job = Job.objects.get(pk=resumed.data["id"])
    assert run.status == "completed", (run.status, run.stop_reason, run.steps_used, run.state, resume_job.status, resume_job.error, resume_job.result)
    assert ActionTask.objects.filter(created_by=users["a"], child_asin="B000TEST01").count() == 1
    assert run.result["write"]["status"] in {"created", "reused"}


def test_investigation_write_tool_is_not_called_when_remaining_budget_cannot_save_result(metric_scope, client, monkeypatch):
    from insight.investigation import commands

    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shops[0].pk),
            "query": "请分析 B000TEST01 并创建核查任务",
            "max_steps": 3,
            "idempotency_key": "investigation-write-budget-1",
        },
        format="json",
    )
    assert response.status_code == 202
    called = {"count": 0}

    def unexpected(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("预算不足时不应调用内部写工具")

    monkeypatch.setattr(commands, "create_draft_task", unexpected)
    run_job(response.data["id"])
    assert called["count"] == 0
    assert not ActionTask.objects.filter(child_asin="B000TEST01").exists()


def test_investigation_negated_write_request_never_creates_internal_task(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shops[0].pk),
            "query": "请调查 B000TEST01 的评论异常，不要创建核查任务",
            "max_steps": 6,
            "idempotency_key": "investigation-no-write-1",
        },
        format="json",
    )
    assert response.status_code == 202
    run_job(response.data["id"])
    run = InvestigationRun.objects.get(
        pk=client.get(f"/api/v1/jobs/{response.data['id']}/").data["result"]["investigation_id"]
    )
    assert run.status == "completed"
    assert not run.result.get("write")
    assert not ActionTask.objects.filter(created_by=users["a"], child_asin="B000TEST01").exists()


def test_investigation_api_opt_in_can_create_internal_task_without_write_phrase(metric_scope, client):
    users, shops = metric_scope
    client.force_authenticate(users["a"])
    response = client.post(
        "/api/v1/investigations/",
        {
            "shop": str(shops[0].pk),
            "query": "请调查 B000TEST01 的评论异常",
            "allow_internal_task": True,
            "max_steps": 8,
            "idempotency_key": "investigation-opt-in-write-1",
        },
        format="json",
    )
    assert response.status_code == 202
    run_job(response.data["id"])
    run = InvestigationRun.objects.get(
        pk=client.get(f"/api/v1/jobs/{response.data['id']}/").data["result"]["investigation_id"]
    )
    assert run.status == "completed"
    assert run.result["write"]["status"] in {"created", "reused"}
    assert ActionTask.objects.filter(created_by=users["a"], child_asin="B000TEST01").count() == 1
