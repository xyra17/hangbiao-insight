from datetime import date, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from insight.ingestion.contracts import digest
from insight.models import (
    BusinessEvent,
    EventEvidence,
    MetricSnapshot,
    Notification,
    ProductMapping,
    RuleConfiguration,
    Shop,
)
from insight.services import audit

from .absolute import calculate_absolute
from .cohort import calculate_cohort
from .comparability import assess_comparability
from .daily import calculate_daily
from .facts import authorize_object, source_manifest
from .observations import record_observation_leads
from .occurrence import calculate_occurrences
from .parameters import DEFAULTS
from .severity import notification_baseline, signal_severity

RULE_VERSION = "v2-2026-09-23.2"


def evaluable_metrics(payload):
    """Distinguish a resolved signal from a signal we can no longer measure."""
    daily = payload["daily"]
    windows = [daily.get("baseline", {}), daily.get("current", {})]
    ready = daily["status"] == "ready"
    metrics = {
        "sales_units": ready and all(w.get("ordered_units") is not None for w in windows),
        "sessions": ready and all(w.get("sessions") is not None for w in windows),
        "unit_session_percent": ready and all(w.get("ratio_status") == "ready" for w in windows),
        "buybox": ready and daily.get("buybox_baseline_daily_mean") is not None,
    }
    inventory = payload["absolute"]["inventory"]
    metrics["availability"] = inventory["status"] == "ready" and (
        inventory.get("available") is not None or bool(inventory.get("listing_status"))
    )
    metrics["page_rating"] = payload["absolute"]["ratings"]["status"] == "ready"
    for section in ("cohort", "occurrence"):
        for kind in ("refunds", "returns"):
            result = payload[section].get(kind, {})
            valid = result.get("status") == "ready"
            if section == "cohort":
                valid = valid and all(
                    result.get(window, {}).get("denominator", 0) > 0 for window in ("baseline", "current")
                )
            metrics[f"{section}_{kind}"] = valid
    return metrics


@transaction.atomic
def scan_object(
    actor, shop, asin, product_id=None, request_id="", today=None, record_events=True, expected_inputs=None
):
    shop = Shop.objects.select_for_update().get(pk=shop.pk)
    authorize_object(actor, shop, asin, product_id)
    if expected_inputs is not None:
        from insight.api_errors import Conflict

        from .versions import metric_inputs

        if expected_inputs != {**metric_inputs(shop), "rule": RULE_VERSION}:
            raise Conflict("回补期间来源、映射或规则已改变；已完成快照保留，请重新提交区间，避免混用版本。")
    local_date = today or timezone.now().astimezone(ZoneInfo(shop.timezone)).date()
    manifest = source_manifest(shop)
    configuration = RuleConfiguration.objects.filter(shop=shop).order_by("-version").first()
    rules = configuration.values if configuration else DEFAULTS
    config_version = configuration.version if configuration else 0
    rule_version = f"{RULE_VERSION}/c{config_version}"
    mappings = ProductMapping.objects.filter(
        product__shop=shop,
        product__child_asin=asin,
        effective_from__lte=local_date,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=local_date))
    if product_id:
        mappings = mappings.filter(product_id=product_id)
    mapping_versions = {str(mapping.pk): mapping.version for mapping in mappings}
    identity = digest(
        {
            "shop": str(shop.pk),
            "asin": asin,
            "product": str(product_id) if product_id else None,
            "manifest": manifest,
            "mapping_versions": mapping_versions,
            "rule": rule_version,
            "date": str(local_date),
            "mode": "live" if record_events else "historical",
        }
    )
    existing = MetricSnapshot.objects.filter(fingerprint=identity).first()
    if existing:
        return existing
    daily = calculate_daily(manifest, asin, local_date, product_id, rules=rules)
    absolute = calculate_absolute(manifest, asin, local_date, product_id, rules=rules)
    cohort = calculate_cohort(manifest, asin, local_date, shop.timezone, product_id, rules=rules)
    occurrence = calculate_occurrences(manifest, asin, local_date, product_id, rules=rules)
    comparability = assess_comparability(manifest, asin, daily, cohort, product_id)
    snapshot = MetricSnapshot.objects.create(
        shop=shop,
        child_asin=asin,
        product_id=product_id,
        actor=actor,
        fingerprint=identity,
        rule_version=rule_version,
        source_manifest=manifest,
        payload={
            "mapping_versions": mapping_versions,
            "daily": daily,
            "absolute": absolute,
            "cohort": cohort,
            "occurrence": occurrence,
            "mode": "live" if record_events else "historical",
            "comparability": comparability,
            "configuration": {"version": config_version, "values": rules},
        },
        local_date=local_date,
    )
    if not record_events:
        audit(
            actor,
            shop,
            "metrics.recomputed",
            snapshot.pk,
            after={"fingerprint": identity, "as_of": str(local_date)},
            request_id=request_id,
        )
        return snapshot
    previous = (
        MetricSnapshot.objects.filter(shop=shop, child_asin=asin, product_id=product_id)
        .exclude(pk=snapshot.pk)
        .exclude(payload__contains={"mode": "historical"})
        .order_by("-created_at")
        .first()
    )
    previous_signals = {}
    if previous and previous.rule_version == rule_version:
        for section in previous.payload.values():
            if isinstance(section, dict):
                for signal in section.get("signals", []):
                    previous_signals[signal["metric"]] = signal
    groups = {}
    for signal in daily["signals"] + absolute["signals"] + cohort["signals"] + occurrence["signals"]:
        signal.setdefault("cutoff", daily.get("cutoff"))
        prior_signal = previous_signals.get(signal["metric"])
        if prior_signal and prior_signal.get("cutoff") <= signal["cutoff"]:
            signal["consecutive_scans"] = prior_signal.get("consecutive_scans", 1) + int(
                prior_signal["cutoff"] < signal["cutoff"]
            )
        else:
            signal["consecutive_scans"] = 1
        groups.setdefault(signal["family"], []).append(signal)
    object_key = str(product_id) if product_id else f"asin:{asin}"
    evaluable = evaluable_metrics(snapshot.payload)
    checked_at = timezone.now()
    for open_event in (
        BusinessEvent.objects.select_for_update()
        .filter(shop=shop, object_key=object_key)
        .exclude(status="closed")
    ):
        measured = open_event.evidence.values_list("metric", flat=True).distinct()
        needs_data = any(not evaluable.get(metric, False) for metric in measured)
        if open_event.needs_data != needs_data:
            open_event.version += 1
        open_event.needs_data = needs_data
        open_event.last_checked_at = checked_at
        open_event.save(update_fields=["needs_data", "last_checked_at", "version"])
    for family, signals in groups.items():
        strongest = min(signals, key=lambda s: (s["priority"], -signal_severity(s)))
        cutoff = max(date.fromisoformat(signal["cutoff"]) for signal in signals)
        event = (
            BusinessEvent.objects.select_for_update()
            .filter(shop=shop, object_key=object_key, family=family)
            .exclude(status="closed")
            .first()
        )
        if event:
            prior = list(
                event.evidence.filter(metric__in=[s["metric"] for s in signals]).select_related("snapshot")
            )
            if all(
                any(
                    e.metric == signal["metric"]
                    and e.detail.get("cutoff") == signal["cutoff"]
                    and e.snapshot.source_manifest == manifest
                    and e.snapshot.rule_version == rule_version
                    for e in prior
                )
                for signal in signals
            ):
                continue
        notification_kind = None
        last_evidence = (
            event.evidence.select_related("snapshot").order_by("-created_at").first() if event else None
        )
        configuration_changed = bool(last_evidence and last_evidence.snapshot.rule_version != rule_version)
        if event is None:
            previous = (
                BusinessEvent.objects.select_for_update()
                .filter(shop=shop, object_key=object_key, family=family, status="closed")
                .order_by("-closed_at")
                .first()
            )
            if previous and cutoff <= previous.last_cutoff:
                # Corrections and configuration changes to an already closed window
                # are revision evidence, not a fresh business recurrence.
                for signal in signals:
                    EventEvidence.objects.get_or_create(
                        event=previous,
                        snapshot=snapshot,
                        metric=signal["metric"],
                        defaults={"detail": {**signal, "evidence_type": "historical_revision"}},
                    )
                audit(
                    actor,
                    shop,
                    "event.history_corrected",
                    previous.pk,
                    after={"snapshot_id": str(snapshot.pk)},
                    request_id=request_id,
                )
                continue
            if previous and previous.closed_at and timezone.now() - previous.closed_at <= timedelta(days=30):
                event = previous
                event.status, event.closed_at = "pending", None
                event.closure_reason, event.closure_note, event.closure_evidence = "", "", []
                event.duplicate_of = None
                event.version += 1
                notification_kind = "reopened"
            else:
                event = BusinessEvent(
                    shop=shop,
                    child_asin=asin,
                    product_id=product_id,
                    object_key=object_key,
                    family=family,
                    last_cutoff=cutoff,
                    reopened_from=previous,
                )
                notification_kind = "new"
        elif not configuration_changed and (
            strongest["priority"] < event.notified_priority
            or signal_severity(strongest) >= notification_baseline(event) * Fraction(5, 4)
        ):
            notification_kind = "worsened"
        if event.snoozed_until and (
            event.snoozed_until <= checked_at or strongest["priority"] < event.priority or family == "safety"
        ):
            audit(
                actor,
                shop,
                "event.snooze_released",
                event.pk,
                before={"snoozed_until": str(event.snoozed_until)},
                after={"snapshot_id": str(snapshot.pk)},
                request_id=request_id,
            )
            event.snoozed_until, event.snooze_reason = None, ""
            event.version += 1
        event.consecutive_days = max(signal["consecutive_scans"] for signal in signals)
        event.title, event.priority = strongest["title"], strongest["priority"]
        event.magnitude, event.impact = strongest["magnitude"], strongest.get("impact")
        event.last_seen, event.last_cutoff = checked_at, cutoff
        event.last_checked_at = checked_at
        if notification_kind in {"new", "reopened"}:
            event.needs_data = any(not evaluable.get(signal["metric"], False) for signal in signals)
        if event.snoozed_until and event.snoozed_until > checked_at and family != "safety":
            notification_kind = None
        if notification_kind or configuration_changed:
            event.notified_magnitude, event.notified_priority = event.magnitude, event.priority
            severity = signal_severity(strongest)
            event.notified_magnitude_ratio = [str(severity.numerator), str(severity.denominator)]
        if not event._state.adding:
            event.version += 1
        event.save()
        for signal in signals:
            EventEvidence.objects.create(
                event=event, snapshot=snapshot, metric=signal["metric"], detail=signal
            )
        if notification_kind:
            recipients = (
                get_user_model()
                .objects.filter(is_active=True, membership__workspace=shop.workspace)
                .distinct()
            )
            for user in recipients:
                from rest_framework.exceptions import PermissionDenied

                try:
                    authorize_object(user, shop, asin, product_id)
                except PermissionDenied:
                    continue
                Notification.objects.get_or_create(
                    user=user, event=event, kind=notification_kind, data_version=snapshot.fingerprint
                )
            audit(
                actor,
                shop,
                f"event.{notification_kind}",
                event.pk,
                after={"snapshot_id": str(snapshot.pk), "family": family},
                request_id=request_id,
            )
    record_observation_leads(snapshot, actor, request_id)
    snapshot.save(update_fields=["payload"])
    audit(actor, shop, "metrics.scanned", snapshot.pk, after={"fingerprint": identity}, request_id=request_id)
    return snapshot
