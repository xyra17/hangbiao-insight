from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from django.utils import timezone

from insight.investigation.budget import (
    acquire_workspace_execution,
    begin_external_attempt,
    begin_workspace_dispatch,
    release_workspace_execution,
    release_workspace_reservation,
    reserve,
    reserve_workspace_call,
)
from insight.models import (
    InvestigationRun,
    Job,
    WorkspaceExecutionLease,
    WorkspaceExecutionPolicy,
    WorkspaceModelBudgetPolicy,
    WorkspaceModelReservation,
)

pytestmark = pytest.mark.django_db


def policy(workspace, actor, *, daily="1", monthly="10", concurrency=2, enabled=True):
    return WorkspaceModelBudgetPolicy.objects.create(
        workspace=workspace,
        enabled=enabled,
        currency="XTS",
        daily_cost_limit=Decimal(daily),
        monthly_cost_limit=Decimal(monthly),
        max_concurrent_external=concurrency,
        lease_seconds=60,
        updated_by=actor,
    )


def make_run(shop, actor, query):
    return InvestigationRun.objects.create(
        shop=shop, requested_by=actor, child_asin="B000TEST01", query=query,
        max_steps=6, budget_units=6,
    )


def test_missing_or_disabled_workspace_policy_blocks_external_reservation(metric_scope, monkeypatch):
    users, shops = metric_scope
    monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "XTS")
    with pytest.raises(RuntimeError, match="workspace_model_budget_policy_missing_or_disabled"):
        reserve_workspace_call(
            workspace_id=shops[0].workspace_id, reservation_key="missing", provider="fake",
            operation="planner", reserved_cost="0.01",
        )
    policy(shops[0].workspace, users["admin"], enabled=False)
    with pytest.raises(RuntimeError, match="workspace_model_budget_policy_missing_or_disabled"):
        reserve_workspace_call(
            workspace_id=shops[0].workspace_id, reservation_key="disabled", provider="fake",
            operation="planner", reserved_cost="0.01",
        )


def test_workspace_budget_is_shared_across_runs_and_idempotent_replay(metric_scope, monkeypatch):
    users, shops = metric_scope
    monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "XTS")
    policy(shops[0].workspace, users["admin"], daily="1", monthly="0.10")
    first_run = make_run(shops[0], users["a"], "first run")
    second_run = make_run(shops[1], users["b"], "second run")
    first = reserve_workspace_call(
        workspace_id=shops[0].workspace_id, reservation_key=f"run:{first_run.pk}", provider="fake",
        operation="planner", reserved_cost="0.06", object_id=str(first_run.pk),
    )
    replay = reserve_workspace_call(
        workspace_id=shops[0].workspace_id, reservation_key=f"run:{first_run.pk}", provider="fake",
        operation="planner", reserved_cost="0.06", object_id=str(first_run.pk),
    )
    assert replay.pk == first.pk
    with pytest.raises(RuntimeError, match="workspace_monthly_model_budget_exhausted"):
        reserve_workspace_call(
            workspace_id=shops[1].workspace_id, reservation_key=f"run:{second_run.pk}", provider="fake",
            operation="planner", reserved_cost="0.05", object_id=str(second_run.pk),
        )
    assert WorkspaceModelReservation.objects.filter(workspace=shops[0].workspace).count() == 1


def test_concurrency_unknown_cost_and_expired_lease_across_investigation_runs(metric_scope, monkeypatch):
    users, shops = metric_scope
    monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "XTS")
    policy(shops[0].workspace, users["admin"], daily="0.20", monthly="1", concurrency=1)
    first_run = make_run(shops[0], users["a"], "first external run")
    second_run = make_run(shops[1], users["b"], "second external run")
    first = reserve(first_run, kind="model", operation="planner", idempotency_key="first", metadata={
        "provider": "fake", "reserved_max_cost": "0.05",
    })
    begin_external_attempt(first)
    first.refresh_from_db()
    workspace_reservation = WorkspaceModelReservation.objects.get(investigation_entry=first)
    assert workspace_reservation.status == "dispatching" and workspace_reservation.dispatch_count == 1
    with pytest.raises(RuntimeError, match="workspace_external_concurrency_limit_reached"):
        second = reserve(second_run, kind="model", operation="planner", idempotency_key="second", metadata={
            "provider": "fake", "reserved_max_cost": "0.05",
        })
        begin_external_attempt(second)
    with pytest.raises(RuntimeError, match="outcome_unknown_requires_new_authorized_run"):
        begin_external_attempt(first)

    # A dispatched call with no receipt keeps its reserved cost. Lease expiry
    # frees the concurrency slot but does not refund that unknown charge.
    workspace_reservation.lease_expires_at = timezone.now() - timedelta(seconds=1)
    workspace_reservation.save(update_fields=["lease_expires_at"])
    second = reserve(second_run, kind="model", operation="planner", idempotency_key="second", metadata={
        "provider": "fake", "reserved_max_cost": "0.05",
    })
    begin_external_attempt(second)
    workspace_reservation.refresh_from_db()
    assert workspace_reservation.status == "unknown" and workspace_reservation.actual_cost is None
    # Unknown is no longer concurrent; the remaining daily amount still covers
    # the second call because its upper bound is accounted for.
    assert WorkspaceModelReservation.objects.filter(workspace=shops[0].workspace, dispatch_count=1).count() == 2


def test_workspace_policy_currency_must_match_runtime_quote(metric_scope, monkeypatch):
    users, shops = metric_scope
    policy(shops[0].workspace, users["admin"], daily="1", monthly="10")
    monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "USD")
    with pytest.raises(RuntimeError, match="workspace_model_budget_currency_mismatch"):
        reserve_workspace_call(
            workspace_id=shops[0].workspace_id, reservation_key="currency", provider="fake",
            operation="planner", reserved_cost="0.01",
        )


def test_released_reservation_replay_rechecks_shared_cost_and_concurrency(metric_scope, monkeypatch):
    users, shops = metric_scope
    monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "XTS")
    policy(shops[0].workspace, users["admin"], daily="1", monthly="10", concurrency=1)
    first = reserve_workspace_call(
        workspace_id=shops[0].workspace_id, reservation_key="same-key", provider="fake",
        operation="planner", reserved_cost="0.6",
    )
    release_workspace_reservation(first, reason="controlled_before_dispatch")
    reserve_workspace_call(
        workspace_id=shops[0].workspace_id, reservation_key="other-key", provider="fake",
        operation="planner", reserved_cost="0.6",
    )
    with pytest.raises(RuntimeError, match="workspace_daily_model_budget_exhausted"):
        reserve_workspace_call(
            workspace_id=shops[0].workspace_id, reservation_key="same-key", provider="fake",
            operation="planner", reserved_cost="0.6",
        )
    first.refresh_from_db()
    assert first.status == "released"
    assert WorkspaceModelReservation.objects.filter(workspace=shops[0].workspace).count() == 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_workspace_reservations_are_serialized(metric_scope, monkeypatch):
    from django.db import close_old_connections

    users, shops = metric_scope
    monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "XTS")
    policy(shops[0].workspace, users["admin"], daily="1", monthly="10", concurrency=2)
    barrier = Barrier(2)

    def attempt(key):
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            return reserve_workspace_call(
                workspace_id=shops[0].workspace_id, reservation_key=key, provider="fake",
                operation="planner", reserved_cost="0.6",
            ).status
        except RuntimeError as exc:
            return str(exc)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["parallel-a", "parallel-b"]))
    assert outcomes.count("reserved") == 1
    assert sum("workspace_daily_model_budget_exhausted" in outcome for outcome in outcomes) == 1
    assert WorkspaceModelReservation.objects.filter(workspace=shops[0].workspace, status="reserved").count() == 1


def test_dispatch_rechecks_disabled_policy_changed_limits_and_timeout_bound(metric_scope, monkeypatch):
    users, shops = metric_scope
    monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "XTS")
    model_policy = policy(shops[0].workspace, users["admin"], daily="1", monthly="10", concurrency=2)
    reservation = reserve_workspace_call(
        workspace_id=shops[0].workspace_id, reservation_key="disabled-before-dispatch", provider="fake",
        operation="planner", reserved_cost="0.1", dispatch_timeout_seconds=120,
    )
    monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "EUR")
    with pytest.raises(RuntimeError, match="workspace_model_budget_currency_mismatch"):
        begin_workspace_dispatch(reservation)
    monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "XTS")
    model_policy.enabled = False
    model_policy.save(update_fields=["enabled"])
    with pytest.raises(RuntimeError, match="workspace_model_budget_policy_missing_or_disabled"):
        begin_workspace_dispatch(reservation)

    model_policy.enabled = True
    model_policy.daily_cost_limit = Decimal("0.05")
    model_policy.save(update_fields=["enabled", "daily_cost_limit"])
    with pytest.raises(RuntimeError, match="workspace_daily_model_budget_exhausted"):
        begin_workspace_dispatch(reservation)

    model_policy.daily_cost_limit = Decimal("1")
    model_policy.lease_seconds = 30
    model_policy.save(update_fields=["daily_cost_limit", "lease_seconds"])
    dispatched = begin_workspace_dispatch(reservation)
    assert dispatched.lease_expires_at >= dispatched.dispatched_at + timedelta(seconds=150)
    from insight.investigation.budget import _expire_workspace_leases
    from insight.models import Workspace
    _expire_workspace_leases(Workspace.objects.get(pk=shops[0].workspace_id), timezone.now())
    dispatched.refresh_from_db()
    assert dispatched.status == "dispatching"


def test_evidence_search_relevance_requires_and_settles_workspace_budget(metric_scope, monkeypatch):
    import json

    from insight.semantic.relevance import CONSTRAINTS, RelevanceAdapter

    users, shops = metric_scope
    monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "XTS")

    class ControlledGateway:
        provenance = "controlled-no-transmission"
        execution_origin = "provider_response"
        timeout_seconds = 15
        last_usage = {"input_tokens": 40, "output_tokens": 20}
        calls = 0

        def plan(self, **kwargs):
            self.calls += 1
            request = json.loads(kwargs["user"])
            return {"request_sha256": request["request_sha256"], "decisions": [{
                "candidate_id": request["candidates"][0]["candidate_id"],
                "label": "irrelevant", "source_field": "text", "start": 0, "end": 0, "quote": "",
                "constraints": {key: "not_applicable" for key in CONSTRAINTS},
            }]}

    gateway = ControlledGateway()
    adapter = RelevanceAdapter(gateway, input_rate="1", output_rate="1", max_output_tokens=100, max_cost="1")
    candidates = [{"fact_id": "fact-1", "title": "", "text": "packaging arrived damaged"}]
    kwargs = {"query": "packaging", "candidates": candidates, "scope": {"shop": str(shops[0].pk)},
              "complete": True, "workspace": shops[0].workspace}
    blocked = adapter.judge(**kwargs)
    assert blocked["complete"] is False and blocked["dispatch_attempted"] is False
    assert gateway.calls == 0
    assert WorkspaceModelReservation.objects.filter(workspace=shops[0].workspace).count() == 0

    policy(shops[0].workspace, users["admin"], daily="1", monthly="10", concurrency=1)
    result = adapter.judge(**kwargs)
    reservation = WorkspaceModelReservation.objects.get(workspace=shops[0].workspace)
    assert result["complete"] is True and result["dispatch_attempted"] is True
    assert gateway.calls == 1 and reservation.status == "settled" and reservation.dispatch_count == 1
    assert reservation.actual_cost == Decimal("0.00006000")
    assert reservation.metadata["relevance_result"]["request_sha256"] == result["request_sha256"]
    cached = adapter.judge(**kwargs)
    assert cached["cache_reused"] is True and gateway.calls == 1


def test_free_investigations_share_workspace_execution_admission(metric_scope, monkeypatch):
    users, shops = metric_scope
    policy = WorkspaceExecutionPolicy.objects.create(
        workspace=shops[0].workspace, max_concurrent_investigations=1,
        lease_seconds=900, updated_by=users["admin"],
    )
    first = Job.objects.create(shop=shops[0], actor=users["a"], kind="investigation_run", idempotency_key="free-1")
    second = Job.objects.create(shop=shops[1], actor=users["b"], kind="refresh_derived", idempotency_key="free-2")
    assert acquire_workspace_execution(first, lease_token=1)
    assert not acquire_workspace_execution(second, lease_token=1)
    lease = WorkspaceExecutionLease.objects.get(job=first)
    assert lease.status == "active" and lease.workspace_id == policy.workspace_id


def test_worker_reenters_workspace_slot_after_saturation_clears(metric_scope):
    from insight.tasks import run_job

    users, shops = metric_scope
    workspace_policy = WorkspaceExecutionPolicy.objects.create(
        workspace=shops[0].workspace,
        max_concurrent_investigations=1,
        lease_seconds=900,
        updated_by=users["admin"],
    )
    occupied = Job.objects.create(
        shop=shops[0], actor=users["a"], kind="investigation_run", idempotency_key="slot-occupied"
    )
    run = InvestigationRun.objects.create(
        shop=shops[1], requested_by=users["b"], child_asin="B000TEST01",
        query="调查 B000TEST01 评论", max_steps=6,
    )
    waiting = Job.objects.create(
        shop=shops[1], actor=users["b"], kind="investigation_run", idempotency_key="slot-waiting",
        payload={"run_id": str(run.pk)},
    )
    assert acquire_workspace_execution(occupied, lease_token=1)

    run_job(str(waiting.pk))
    waiting.refresh_from_db()
    assert waiting.status == "retry_wait" and waiting.attempts == 0
    assert waiting.error == "workspace_investigation_concurrency_limit_reached"
    waiting_lease = WorkspaceExecutionLease.objects.get(job=waiting)
    assert waiting_lease.status == "active" and waiting_lease.lease_expires_at is None

    release_workspace_execution(occupied, lease_token=1)
    Job.objects.filter(pk=waiting.pk).update(next_run_at=timezone.now() - timedelta(seconds=1))
    run_job(str(waiting.pk))

    waiting.refresh_from_db()
    run.refresh_from_db()
    waiting_lease.refresh_from_db()
    assert waiting.status == "completed" and waiting.attempts == 1
    assert run.status in {"completed", "partial"}
    assert waiting.result["status"] == run.status
    assert waiting_lease.workspace_id == workspace_policy.workspace_id
    assert waiting_lease.status == "released"

