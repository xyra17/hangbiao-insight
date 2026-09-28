"""Durable investigation budget and tool-call accounting.

Every planner attempt consumes a unit before work starts.  The entry is
idempotent, so a worker crash cannot refund or double-spend the allowance when
the same checkpoint is replayed.  Tokens, cost and duration are settled later
on the same append-only row.
"""

from __future__ import annotations

from decimal import Decimal
from time import monotonic

from django.db import transaction

from insight.api_errors import Conflict
from insight.models import InvestigationBudgetLedger, InvestigationRun


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
        return locked


class MeasuredCall:
    """Small helper used by planner/tool adapters to settle duration reliably."""

    def __init__(self, entry: InvestigationBudgetLedger):
        self.entry = entry
        self.started = monotonic()

    def finish(self, *, tokens: int = 0, cost: Decimal | str | float = 0, metadata: dict | None = None):
        return settle(self.entry, tokens=tokens, cost=cost,
                      duration_ms=int((monotonic() - self.started) * 1000), metadata=metadata)
