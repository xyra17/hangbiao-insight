from django.utils import timezone

from insight.ingestion.contracts import digest
from insight.models import ObservationLead


def record_observation_leads(snapshot, actor=None, request_id=""):
    """Persist low-sample review lines without creating events or notifications."""
    payload = snapshot.payload
    object_key = str(snapshot.product_id) if snapshot.product_id else f"asin:{snapshot.child_asin}"
    for kind in ("refunds", "returns"):
        metric = payload.get("cohort", {}).get(kind, {})
        observation = metric.get("observation", {})
        if observation.get("status") != "low_sample":
            continue
        cutoff = metric.get("cutoff") or "unknown"
        fingerprint = digest(
            {
                "shop": str(snapshot.shop_id),
                "object": object_key,
                "metric": f"cohort_{kind}",
                "cutoff": cutoff,
                "rule": snapshot.rule_version,
            }
        )
        ObservationLead.objects.update_or_create(
            fingerprint=fingerprint,
            defaults={
                "shop": snapshot.shop,
                "child_asin": snapshot.child_asin,
                "object_key": object_key,
                "product_id": snapshot.product_id,
                "family": "experience",
                "metric": f"cohort_{kind}",
                "title": "成熟队列样本不足，保留观察线索",
                "snapshot": snapshot,
                "detail": {
                    "status": metric.get("status"),
                    "observation": observation,
                    "cutoff": cutoff,
                    "message": metric.get("reason"),
                },
                "last_seen": timezone.now(),
            },
        )
