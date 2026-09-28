"""Safe, auditable retention cleanup for raw files and derived indexes."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from insight.models import (
    AuditEntry,
    ImportBatch,
    RawFile,
    RetentionTombstone,
    Review,
    SemanticClaim,
    Shop,
    SupportCase,
)
from insight.services import audit

DEFAULT_RETENTION_DAYS = 180


def _tombstone(workspace, object_type, object_id, *, content_hash="", reason="retention"):
    return RetentionTombstone.objects.get_or_create(
        workspace=workspace,
        object_type=object_type,
        object_id=str(object_id),
        defaults={
            "content_hash": content_hash,
            "deleted_at": timezone.now(),
            "reason": reason,
            "placeholder": {"object_type": object_type, "object_id": str(object_id), "payload": "redacted"},
        },
    )


def cleanup_retention(*, now=None, dry_run=False, workspace_id=None, actor=None):
    """Apply the shortest of global and source contract retention windows.

    Raw payload files are removed from storage only after a tombstone is
    written.  Import metadata, facts referenced by reports/claims, and audit
    relationships remain addressable; an absent payload is represented by the
    tombstone and a redacted RawFile row.  This makes cleanup reversible at the
    metadata level and prevents broken evidence links.
    """
    now = now or timezone.now()
    global_days = int(os.getenv("INSIGHT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    if global_days < 1:
        raise ValueError("INSIGHT_RETENTION_DAYS must be positive")
    shops = Shop.objects.select_related("workspace").all()
    if workspace_id:
        shops = shops.filter(workspace_id=workspace_id)
    summary = {"dry_run": dry_run, "raw_files": 0, "indexes": 0, "audit_entries": 0, "tombstones": 0, "cutoffs": []}
    for shop in shops:
        eligible_batches = []
        retention_values = []
        for batch in ImportBatch.objects.filter(contract__source__shop=shop).select_related(
            "contract", "raw_file"
        ):
            days = min(global_days, batch.contract.retention_days or DEFAULT_RETENTION_DAYS)
            cutoff = now - timedelta(days=days)
            if batch.created_at < cutoff:
                eligible_batches.append(batch)
                retention_values.append(days)
        summary["cutoffs"].append(
            {
                "shop": str(shop.pk),
                "retention_days_min": min(retention_values) if retention_values else global_days,
                "eligible_batches": len(eligible_batches),
            }
        )
        eligible_batch_ids = {batch.pk for batch in eligible_batches}
        raw_ids = {batch.raw_file_id for batch in eligible_batches}
        pending_ids = set(
            RawFile.objects.filter(deletion_reason="保留期清理待删除", storage_key__gt="")
            .filter(pk__in=raw_ids)
            .values_list("pk", flat=True)
        )
        raw_ids |= pending_ids
        if dry_run:
            summary["raw_files"] += RawFile.objects.filter(pk__in=raw_ids).exclude(
                deletion_reason="保留期清理完成"
            ).count()
        else:
            for raw_id in sorted(raw_ids, key=str):
                with transaction.atomic():
                    raw = RawFile.objects.select_for_update().get(pk=raw_id)
                    if raw.deletion_reason == "保留期清理完成" or not raw.storage_key:
                        continue
                    _tombstone(
                        shop.workspace,
                        "raw_file",
                        raw.pk,
                        content_hash=raw.sha256,
                        reason="retention raw payload",
                    )
                    if raw.deleted_at is None:
                        raw.deleted_at = now
                    raw.deletion_reason = "保留期清理待删除"
                    raw.save(update_fields=["deleted_at", "deletion_reason"])
                    storage_key = raw.storage_key
                # The tombstone and deny-download marker commit before touching
                # the filesystem. A crash or unlink error is retried on the
                # next cleanup run while the stored key remains available.
                (Path(settings.STORAGE_ROOT) / storage_key).unlink(missing_ok=True)
                with transaction.atomic():
                    raw = RawFile.objects.select_for_update().get(pk=raw_id)
                    if raw.storage_key == storage_key and raw.deletion_reason == "保留期清理待删除":
                        raw.deletion_reason = "保留期清理完成"
                        raw.storage_key = ""
                        raw.size = 0
                        raw.save(update_fields=["deletion_reason", "storage_key", "size"])
                        summary["raw_files"] += 1

        # Index payloads are safe to rebuild and are cleared only when no claim
        # points to the review. Claims keep the review placeholder addressable.
        claimed_reviews = SemanticClaim.objects.filter(
            review__batch_id__in=eligible_batch_ids
        ).values("review_id")
        old_reviews = Review.objects.filter(source__shop=shop, batch_id__in=eligible_batch_ids).exclude(pk__in=claimed_reviews)
        old_support = SupportCase.objects.filter(source__shop=shop, batch_id__in=eligible_batch_ids)
        if dry_run:
            summary["indexes"] += old_reviews.count() + old_support.count()
        else:
            summary["indexes"] += old_reviews.update(search_tokens="", search_vector=None)
            summary["indexes"] += old_support.update(search_tokens="", search_vector=None)

        audit_cutoff = now - timedelta(days=global_days)
        old_audit = AuditEntry.objects.filter(shop=shop, created_at__lt=audit_cutoff)
        if dry_run:
            summary["audit_entries"] += old_audit.count()
        else:
            summary["audit_entries"] += old_audit.count()
            old_audit.delete()
        if not dry_run:
            audit(actor, shop, "retention.completed", shop.pk,
                  after={"cutoff": audit_cutoff.isoformat(), **{key: value for key, value in summary.items() if key in {"raw_files", "indexes", "audit_entries"}}})
    summary["tombstones"] = RetentionTombstone.objects.filter(created_at__gte=now - timedelta(minutes=1)).count() if not dry_run else 0
    return summary
