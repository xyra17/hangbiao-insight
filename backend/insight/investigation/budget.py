"""Durable investigation budget and tool-call accounting.

Every planner attempt consumes a unit before work starts.  The entry is
idempotent, so a worker crash cannot refund or double-spend the allowance when
the same checkpoint is replayed.  Tokens, cost and duration are settled later
on the same append-only row.
"""

from __future__ import annotations

import math
import os
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from time import monotonic
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from insight.api_errors import Conflict
from insight.models import (
    InvestigationBudgetLedger,
    InvestigationRun,
    Workspace,
    WorkspaceExecutionLease,
    WorkspaceExecutionPolicy,
    WorkspaceModelBudgetPolicy,
    WorkspaceModelReservation,
)


def reserve(run: InvestigationRun, *, kind: str, operation: str, idempotency_key: str,
            units: int = 1, metadata: dict | None = None) -> InvestigationBudgetLedger:
    if units < 0:
        raise ValueError("budget units must be non-negative")
    with transaction.atomic():
        locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
        existing = InvestigationBudgetLedger.objects.filter(
            run=locked, idempotency_key=idempotency_key
        ).first()
        if existing:
            return existing
        spent = sum(item.used_units for item in InvestigationBudgetLedger.objects.filter(run=locked))
        if spent + units > locked.max_steps:
            raise Conflict("调查累计预算已用完，接续不会重置预算。")
        sequence = (InvestigationBudgetLedger.objects.filter(run=locked).order_by("-sequence").values_list("sequence", flat=True).first() or 0) + 1
        entry = InvestigationBudgetLedger.objects.create(
            run=locked,
            sequence=sequence,
            kind=kind,
            operation=operation,
            reserved_units=units,
            used_units=units,
            idempotency_key=idempotency_key,
            metadata=metadata or {},
        )
        locked.model_attempts = InvestigationBudgetLedger.objects.filter(run=locked, kind="model").count()
        # ``max_steps`` bounds durable graph checkpoints.  Model attempts are
        # accounted for separately in the append-only ledger so a planner call
        # cannot consume the same allowance a second time before its node
        # checkpoint is written.
        locked.budget_units = max(locked.max_steps - locked.steps_used, 0)
        locked.save(update_fields=["model_attempts", "budget_units"])
        return entry


def settle(entry: InvestigationBudgetLedger, *, tokens: int = 0, cost: Decimal | str | float = 0,
           duration_ms: int = 0, metadata: dict | None = None) -> InvestigationBudgetLedger:
    with transaction.atomic():
        locked = InvestigationBudgetLedger.objects.select_for_update().get(pk=entry.pk)
        locked.tokens = max(0, int(tokens))
        locked.cost = Decimal(str(cost))
        locked.duration_ms = max(0, int(duration_ms))
        if metadata:
            locked.metadata = {**locked.metadata, **metadata}
        locked.save(update_fields=["tokens", "cost", "duration_ms", "metadata"])
        run = InvestigationRun.objects.select_for_update().get(pk=locked.run_id)
        totals = InvestigationBudgetLedger.objects.filter(run=run)
        run.model_tokens = sum(item.tokens for item in totals)
        run.model_cost = sum((item.cost for item in totals), Decimal("0"))
        run.elapsed_ms = sum(item.duration_ms for item in totals)
        run.duplicate_queries = sum(
            int(item.metadata.get("duplicate", 0)) for item in totals if isinstance(item.metadata, dict)
        )
        run.save(update_fields=["model_tokens", "model_cost", "elapsed_ms", "duplicate_queries"])
        reservation = WorkspaceModelReservation.objects.filter(investigation_entry=locked).first()
        if reservation:
            known = (metadata or {}).get("charge_outcome") == "recorded_by_usage" or (metadata or {}).get("cost_known") is True
            settle_workspace_reservation(reservation, actual_cost=locked.cost, cost_known=known)
        return locked


class MeasuredCall:
    """Small helper used by planner/tool adapters to settle duration reliably."""

    def __init__(self, entry: InvestigationBudgetLedger):
        self.entry = entry
        self.started = monotonic()

    def finish(self, *, tokens: int = 0, cost: Decimal | str | float = 0, metadata: dict | None = None):
        return settle(self.entry, tokens=tokens, cost=cost,
                      duration_ms=int((monotonic() - self.started) * 1000), metadata=metadata)


def begin_external_attempt(entry: InvestigationBudgetLedger):
    """Reserve workspace cost/concurrency, then allow exactly one external dispatch."""
    current = InvestigationBudgetLedger.objects.select_related("run__shop").get(pk=entry.pk)
    if current.metadata.get("external_dispatch_started"):
        raise RuntimeError("external_attempt_outcome_unknown_requires_new_authorized_run")
    reserved_cost = current.metadata.get("reserved_max_cost")
    if reserved_cost is None:
        raise RuntimeError("external_call_cost_upper_bound_missing")
    workspace_reservation = reserve_workspace_call(
        workspace_id=current.run.shop.workspace_id,
        reservation_key=f"investigation:{current.pk}",
        provider=str(current.metadata.get("provider", "unknown")),
        operation=current.operation,
        reserved_cost=reserved_cost,
        dispatch_timeout_seconds=current.metadata.get("dispatch_timeout_seconds", 0),
        investigation_entry=current,
        object_type="investigation_run",
        object_id=str(current.run_id),
    )
    try:
        begin_workspace_dispatch(workspace_reservation)
    except Exception as exc:
        release_workspace_reservation(workspace_reservation, reason=f"dispatch_revalidation:{type(exc).__name__}")
        raise
    with transaction.atomic():
        locked = InvestigationBudgetLedger.objects.select_for_update().get(pk=entry.pk)
        if locked.metadata.get('external_dispatch_started'):
            raise RuntimeError('external_attempt_outcome_unknown_requires_new_authorized_run')
        locked.metadata = {**locked.metadata, 'external_dispatch_started': True,
                           'charge_outcome': 'unknown_until_receipt',
                           'workspace_reservation_id': str(workspace_reservation.pk)}
        locked.save(update_fields=['metadata'])


def _positive_amount(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("external_call_cost_upper_bound_invalid") from exc
    if not amount.is_finite() or amount <= 0:
        raise RuntimeError("external_call_cost_upper_bound_invalid")
    return amount.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)


def _expire_workspace_leases(workspace, now):
    expired = WorkspaceModelReservation.objects.select_for_update().filter(
        workspace=workspace,
        status__in=["reserved", "dispatching"],
        lease_expires_at__lte=now,
    )
    for reservation in expired:
        if reservation.status == "dispatching":
            reservation.status = "unknown"
            reservation.metadata = {**reservation.metadata, "charge_outcome": "unknown_until_receipt",
                                    "lease_expired": True}
        else:
            reservation.status = "released"
            reservation.metadata = {**reservation.metadata, "charge_outcome": "not_dispatched",
                                    "lease_expired": True}
        reservation.lease_expires_at = None
        reservation.settled_at = now
        reservation.save(update_fields=["status", "metadata", "lease_expires_at", "settled_at"])


def _workspace_capacity(workspace, policy, *, day, month, upper, now, exclude_reservation_id=None):
    base = WorkspaceModelReservation.objects.filter(
        workspace=workspace,
        status__in=["reserved", "dispatching", "settled", "settled_overrun", "unknown"],
    )
    if exclude_reservation_id is not None:
        base = base.exclude(pk=exclude_reservation_id)
    daily = _committed_cost(base.filter(budget_day=day))
    monthly = _committed_cost(base.filter(budget_month=month))
    if daily + upper > policy.daily_cost_limit:
        raise RuntimeError("workspace_daily_model_budget_exhausted")
    if monthly + upper > policy.monthly_cost_limit:
        raise RuntimeError("workspace_monthly_model_budget_exhausted")
    active = WorkspaceModelReservation.objects.filter(
        workspace=workspace,
        status__in=["reserved", "dispatching"],
        lease_expires_at__gt=now,
    )
    if exclude_reservation_id is not None:
        active = active.exclude(pk=exclude_reservation_id)
    if active.count() >= policy.max_concurrent_external:
        raise RuntimeError("workspace_external_concurrency_limit_reached")


def _dispatch_lease_seconds(policy, dispatch_timeout_seconds):
    try:
        timeout = float(dispatch_timeout_seconds or 0)
    except (TypeError, ValueError):
        raise RuntimeError("external_call_timeout_bound_invalid") from None
    if not math.isfinite(timeout) or timeout < 0:
        raise RuntimeError("external_call_timeout_bound_invalid")
    return max(policy.lease_seconds, math.ceil(timeout) + 30)


def _committed_cost(queryset):
    total = Decimal("0")
    for item in queryset:
        if item.status in {"settled", "settled_overrun"} and item.actual_cost is not None:
            total += item.actual_cost
        else:
            # Unknown charges keep the full upper-bound reservation until an
            # actual receipt resolves them; they are never silently refunded.
            total += item.reserved_cost
    return total


def reserve_workspace_call(*, workspace_id, reservation_key, provider, operation, reserved_cost,
                           investigation_entry=None, object_type="", object_id="", metadata=None,
                           dispatch_timeout_seconds=0):
    """Atomically reserve shared workspace daily/monthly budget and a call lease."""
    upper = _positive_amount(reserved_cost)
    currency = os.getenv("INVESTIGATION_COST_CURRENCY", "").strip().upper()
    if len(currency) != 3:
        raise RuntimeError("approved_workspace_model_cost_currency_required")
    now = timezone.now()
    with transaction.atomic():
        workspace = Workspace.objects.select_for_update().get(pk=workspace_id)
        policy = WorkspaceModelBudgetPolicy.objects.select_for_update().filter(workspace=workspace).first()
        if policy is None or not policy.enabled:
            raise RuntimeError("workspace_model_budget_policy_missing_or_disabled")
        if policy.currency != currency:
            raise RuntimeError("workspace_model_budget_currency_mismatch")
        _expire_workspace_leases(workspace, now)
        local_day = now.astimezone(ZoneInfo(workspace.timezone)).date()
        local_month = local_day.replace(day=1)
        lease_seconds = _dispatch_lease_seconds(policy, dispatch_timeout_seconds)
        try:
            dispatch_timeout_seconds = max(0, float(dispatch_timeout_seconds or 0))
        except (TypeError, ValueError):
            raise RuntimeError("external_call_timeout_bound_invalid") from None
        reservation_metadata = {
            **(metadata or {}),
            "dispatch_timeout_seconds": dispatch_timeout_seconds,
        }
        existing = WorkspaceModelReservation.objects.select_for_update().filter(
            workspace=workspace, reservation_key=reservation_key
        ).first()
        if existing:
            if (existing.provider, existing.operation, existing.currency, existing.reserved_cost) != (
                provider, operation, currency, upper
            ):
                raise RuntimeError("workspace_reservation_idempotency_payload_mismatch")
            if existing.status == "released":
                _workspace_capacity(
                    workspace,
                    policy,
                    day=local_day,
                    month=local_month,
                    upper=upper,
                    now=now,
                    exclude_reservation_id=existing.pk,
                )
                existing.status = "reserved"
                existing.budget_day = local_day
                existing.budget_month = local_month
                existing.lease_expires_at = now + timedelta(seconds=lease_seconds)
                existing.settled_at = None
                existing.investigation_entry = investigation_entry
                existing.metadata = {**existing.metadata, **reservation_metadata, "reacquired_before_dispatch": True}
                existing.save(update_fields=["status", "budget_day", "budget_month", "lease_expires_at",
                                             "settled_at", "investigation_entry", "metadata"])
                return existing
            if existing.status == "reserved" and existing.lease_expires_at and existing.lease_expires_at > now:
                return existing
            raise RuntimeError("workspace_reservation_already_dispatched_or_outcome_unknown")

        _workspace_capacity(workspace, policy, day=local_day, month=local_month, upper=upper, now=now)
        return WorkspaceModelReservation.objects.create(
            workspace=workspace,
            investigation_entry=investigation_entry,
            reservation_key=reservation_key,
            provider=provider,
            operation=operation,
            currency=currency,
            budget_day=local_day,
            budget_month=local_month,
            reserved_cost=upper,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            object_type=object_type,
            object_id=object_id,
            metadata=reservation_metadata,
        )


def begin_workspace_dispatch(reservation):
    with transaction.atomic():
        current = WorkspaceModelReservation.objects.only("workspace_id").get(pk=reservation.pk)
        workspace = Workspace.objects.select_for_update().get(pk=current.workspace_id)
        policy = WorkspaceModelBudgetPolicy.objects.select_for_update().filter(workspace=workspace).first()
        if policy is None or not policy.enabled:
            raise RuntimeError("workspace_model_budget_policy_missing_or_disabled")
        currency = os.getenv("INVESTIGATION_COST_CURRENCY", "").strip().upper()
        if len(currency) != 3:
            raise RuntimeError("approved_workspace_model_cost_currency_required")
        if policy.currency != currency:
            raise RuntimeError("workspace_model_budget_currency_mismatch")
        _expire_workspace_leases(workspace, timezone.now())
        locked = WorkspaceModelReservation.objects.select_for_update().get(pk=reservation.pk)
        if locked.status != "reserved" or locked.dispatch_count:
            raise RuntimeError("workspace_reservation_already_dispatched_or_outcome_unknown")
        now = timezone.now()
        if not locked.lease_expires_at or locked.lease_expires_at <= now:
            locked.status = "released"
            locked.lease_expires_at = None
            locked.settled_at = now
            locked.save(update_fields=["status", "lease_expires_at", "settled_at"])
            raise RuntimeError("workspace_external_reservation_lease_expired")
        if locked.currency != policy.currency:
            raise RuntimeError("workspace_model_budget_currency_mismatch")
        local_day = now.astimezone(ZoneInfo(workspace.timezone)).date()
        local_month = local_day.replace(day=1)
        if (locked.budget_day, locked.budget_month) != (local_day, local_month):
            locked.status = "released"
            locked.lease_expires_at = None
            locked.settled_at = now
            locked.metadata = {**locked.metadata, "charge_outcome": "not_dispatched", "release_reason": "budget_period_changed"}
            locked.save(update_fields=["status", "lease_expires_at", "settled_at", "metadata"])
            raise RuntimeError("workspace_reservation_budget_period_changed")
        # Recheck the current limits and provider currency immediately before
        # the external request. Reservations remain charged against the cap.
        daily = _committed_cost(WorkspaceModelReservation.objects.filter(
            workspace=workspace, budget_day=local_day,
            status__in=["reserved", "dispatching", "settled", "settled_overrun", "unknown"],
        ))
        monthly = _committed_cost(WorkspaceModelReservation.objects.filter(
            workspace=workspace, budget_month=local_month,
            status__in=["reserved", "dispatching", "settled", "settled_overrun", "unknown"],
        ))
        if daily > policy.daily_cost_limit:
            raise RuntimeError("workspace_daily_model_budget_exhausted")
        if monthly > policy.monthly_cost_limit:
            raise RuntimeError("workspace_monthly_model_budget_exhausted")
        active_count = WorkspaceModelReservation.objects.filter(
            workspace=workspace, status__in=["reserved", "dispatching"], lease_expires_at__gt=now,
        ).count()
        if active_count > policy.max_concurrent_external:
            raise RuntimeError("workspace_external_concurrency_limit_reached")
        locked.status = "dispatching"
        locked.dispatch_count = 1
        locked.dispatched_at = now
        locked.lease_expires_at = now + timedelta(seconds=_dispatch_lease_seconds(
            policy, locked.metadata.get("dispatch_timeout_seconds", 0)
        ))
        locked.save(update_fields=["status", "dispatch_count", "dispatched_at", "lease_expires_at"])
        return locked


def release_workspace_reservation(reservation, *, reason):
    with transaction.atomic():
        locked = WorkspaceModelReservation.objects.select_for_update().get(pk=reservation.pk)
        if locked.status == "reserved" and locked.dispatch_count == 0:
            locked.status = "released"
            locked.lease_expires_at = None
            locked.settled_at = timezone.now()
            locked.metadata = {**locked.metadata, "charge_outcome": "not_dispatched", "release_reason": reason}
            locked.save(update_fields=["status", "lease_expires_at", "settled_at", "metadata"])
        return locked


def settle_workspace_reservation(reservation, *, actual_cost, cost_known, metadata=None):
    with transaction.atomic():
        locked = WorkspaceModelReservation.objects.select_for_update().get(pk=reservation.pk)
        if locked.status in {"settled", "settled_overrun"}:
            return locked
        if locked.status not in {"dispatching", "unknown"}:
            return locked
        now = timezone.now()
        locked.lease_expires_at = None
        locked.settled_at = now
        if cost_known:
            actual = Decimal(str(actual_cost)).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)
            locked.actual_cost = actual
            locked.status = "settled_overrun" if actual > locked.reserved_cost else "settled"
            locked.metadata = {**locked.metadata, **(metadata or {}), "charge_outcome": "recorded_by_usage"}
        else:
            locked.actual_cost = None
            locked.status = "unknown"
            locked.metadata = {**locked.metadata, **(metadata or {}), "charge_outcome": "unknown_until_receipt"}
        locked.save(update_fields=["actual_cost", "status", "metadata", "lease_expires_at", "settled_at"])
        return locked


def _execution_policy_defaults():
    try:
        max_concurrent = min(100, max(1, int(os.getenv("WORKSPACE_MAX_CONCURRENT_INVESTIGATIONS", "4"))))
    except (TypeError, ValueError):
        max_concurrent = 4
    try:
        lease_seconds = min(3600, max(60, int(os.getenv("WORKSPACE_EXECUTION_LEASE_SECONDS", "900"))))
    except (TypeError, ValueError):
        lease_seconds = 900
    return {"max_concurrent_investigations": max_concurrent, "lease_seconds": lease_seconds}


def _execution_lease_seconds(policy):
    from app.agent.runtime_config import InvestigationRuntimeConfig

    config = InvestigationRuntimeConfig()
    bounded_run = max(config.investigation_timeout_seconds, config.planner_timeout_seconds)
    return max(policy.lease_seconds, math.ceil(bounded_run) + 60)


def acquire_workspace_execution(job, *, lease_token):
    """Atomically admit one investigation/derived-refresh worker per workspace slot.

    Deterministic and otherwise free investigations use this same execution
    limit. Model billing is separately gated by WorkspaceModelBudgetPolicy.
    """
    if job.kind not in {"investigation_run", "refresh_derived"}:
        return True
    from insight.models import Job

    now = timezone.now()
    workspace_id = job.shop.workspace_id
    with transaction.atomic():
        workspace = Workspace.objects.select_for_update().get(pk=workspace_id)
        defaults = _execution_policy_defaults()
        WorkspaceExecutionPolicy.objects.get_or_create(workspace=workspace, defaults=defaults)
        policy = WorkspaceExecutionPolicy.objects.select_for_update().get(workspace=workspace)
        lease_seconds = _execution_lease_seconds(policy)

        # A fresh worker heartbeat extends even an unexpectedly short lease;
        # only a dead/stalled worker may lose its slot after the lease expires.
        recent_cutoff = now - timedelta(seconds=60)
        stale = WorkspaceExecutionLease.objects.select_for_update().filter(
            workspace=workspace, status="active", lease_expires_at__lte=now,
        )
        for lease in stale:
            running = Job.objects.filter(pk=lease.job_id, status="running", heartbeat_at__gte=recent_cutoff).exists()
            if running:
                lease.lease_expires_at = now + timedelta(seconds=lease_seconds)
                lease.heartbeat_at = now
                lease.save(update_fields=["lease_expires_at", "heartbeat_at"])
            else:
                lease.status = "released"
                lease.lease_expires_at = None
                lease.released_at = now
                lease.metadata = {**lease.metadata, "lease_expired": True}
                lease.save(update_fields=["status", "lease_expires_at", "released_at", "metadata"])

        lease, _ = WorkspaceExecutionLease.objects.select_for_update().get_or_create(
            job=job,
            defaults={"workspace": workspace, "job_lease_token": lease_token},
        )
        if lease.workspace_id != workspace.pk:
            raise RuntimeError("workspace_execution_lease_scope_mismatch")
        if lease.status == "active" and lease.lease_expires_at and lease.lease_expires_at > now:
            lease.job_lease_token = lease_token
            lease.heartbeat_at = now
            lease.lease_expires_at = now + timedelta(seconds=lease_seconds)
            lease.save(update_fields=["job_lease_token", "heartbeat_at", "lease_expires_at"])
            return True

        active = WorkspaceExecutionLease.objects.filter(
            workspace=workspace, status="active", lease_expires_at__gt=now,
        ).exclude(job=job).count()
        if active >= policy.max_concurrent_investigations:
            return False
        lease.status = "active"
        lease.job_lease_token = lease_token
        lease.lease_expires_at = now + timedelta(seconds=lease_seconds)
        lease.heartbeat_at = now
        lease.released_at = None
        lease.metadata = {**lease.metadata, "admitted_by": "workspace_execution_policy"}
        lease.save(update_fields=["status", "job_lease_token", "lease_expires_at", "heartbeat_at", "released_at", "metadata"])
        return True


def renew_workspace_execution(job, *, lease_token):
    now = timezone.now()
    lease = WorkspaceExecutionLease.objects.filter(
        job=job, status="active", job_lease_token=lease_token, lease_expires_at__gt=now,
    ).select_related("workspace").first()
    if lease is None:
        raise RuntimeError("workspace_execution_lease_lost")
    policy = WorkspaceExecutionPolicy.objects.filter(workspace=lease.workspace).first()
    if policy is None:
        raise RuntimeError("workspace_execution_policy_missing")
    expires_at = now + timedelta(seconds=_execution_lease_seconds(policy))
    updated = WorkspaceExecutionLease.objects.filter(
        pk=lease.pk, status="active", job_lease_token=lease_token, lease_expires_at__gt=now,
    ).update(heartbeat_at=now, lease_expires_at=expires_at)
    if not updated:
        raise RuntimeError("workspace_execution_lease_lost")


def release_workspace_execution(job, *, lease_token):
    now = timezone.now()
    WorkspaceExecutionLease.objects.filter(
        job=job, status="active", job_lease_token=lease_token,
    ).update(status="released", lease_expires_at=None, released_at=now)
