from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import ValidationError

from insight.access import require_role
from insight.metrics.facts import authorize_object
from insight.models import MetricSnapshot, SemanticRun, Shop, StandardReport
from insight.services import audit

from .diagnosis import build_diagnostic_payload, validate_diagnostic_payload
from .service import report_publication_key


def _claim_payload(claim):
    return {
        "claim_id": str(claim.pk),
        "review_id": str(claim.review.record_id),
        "topic": {"primary": claim.primary_topic, "secondary": claim.secondary_topic},
        "polarity": claim.polarity,
        "evidence_span": claim.evidence_span,
        "status": claim.prediction_status,
        "quality_flags": claim.quality_flags,
        "source_refs": [{"type": "raw_review", "raw_row_id": str(claim.review.raw_row_id)}],
    }


@transaction.atomic
def compose_report(
    actor,
    shop,
    asin,
    snapshot: MetricSnapshot | None,
    semantic_run: SemanticRun | None,
    product_id=None,
    request_id="",
):
    Shop.objects.select_for_update().get(pk=shop.pk)
    require_role(actor, shop, "operator", "quality", "support", "data_admin")
    authorize_object(actor, shop, asin, product_id)
    for material in (snapshot, semantic_run):
        if material is not None and (
            material.shop_id != shop.pk
            or material.child_asin != asin
            or str(material.product_id or "") != str(product_id or "")
        ):
            raise ValidationError("报告材料必须属于同一店铺与商品范围。")
    if snapshot and semantic_run and snapshot.source_manifest != semantic_run.source_manifest:
        raise ValidationError("指标与评论采用不同来源版本，须用同一输入快照重新生成。")
    key = report_publication_key(shop, asin, snapshot, semantic_run, product_id)
    if existing := StandardReport.objects.filter(publication_key=key).first():
        return existing, False
    previous = (
        StandardReport.objects.filter(shop=shop, child_asin=asin, product_id=product_id)
        .order_by("-report_version")
        .first()
    )
    claims = list(
        semantic_run.claims.filter(prediction_status="accepted").select_related("review")
        if semantic_run
        else []
    )
    signals = []
    if snapshot:
        for section in ("daily", "absolute", "cohort", "occurrence"):
            signals.extend(snapshot.payload.get(section, {}).get("signals", []))
    diagnosis = build_diagnostic_payload(snapshot, semantic_run)
    guard = validate_diagnostic_payload(diagnosis, snapshot, semantic_run)
    if not guard["valid"]:
        # Evidence guard failure must fail closed: retain deterministic metrics and
        # scenario gaps, but never publish an unsupported causal candidate.
        diagnosis["candidates"] = []
        diagnosis["limitations"].extend(["证据校验失败，已降级为规则事实报告。", *guard["errors"]])
    payload = {
        "status": (
            "complete"
            if snapshot
            and semantic_run
            and semantic_run.status == "completed"
            and all(s["status"] == "ready" for s in diagnosis["scenarios"].values()) and guard["valid"]
            else "partial"
            if snapshot or semantic_run
            else "insufficient_data"
        ),
        "summary": "标准规则和已通过证据校验的评论事实已整理。" if snapshot or claims else "缺少可发布材料。",
        "signals": signals,
        "claims": [_claim_payload(claim) for claim in claims],
        "limitations": [
            "评论主题只代表已采集且通过证据校验的文本，不等同于全部买家意见。",
            "候选信号不自动证明因果；运营事件和数据覆盖仍需核查。",
        ],
        "gaps": [f"{s['label']}：{gap}" for s in diagnosis["scenarios"].values() for gap in s["gaps"]]
        + ([semantic_run.error] if semantic_run and semantic_run.error else [])
        + (
            ["语义适配器尚未完成，未发布模型 claim。"]
            if semantic_run and semantic_run.status == "deferred"
            else []
        ),
        "diagnosis": diagnosis,
        "evidence_guard": guard,
        "citations": [
            {"type": "metric_snapshot", "id": str(snapshot.pk), "fingerprint": snapshot.fingerprint}
            for snapshot in [snapshot]
            if snapshot
        ]
        + [
            {
                "type": "semantic_run",
                "id": str(semantic_run.pk),
                "taxonomy_version": semantic_run.taxonomy_version,
            }
            for semantic_run in [semantic_run]
            if semantic_run
        ],
    }
    report, created = StandardReport.objects.get_or_create(
        publication_key=key,
        defaults={
            "shop": shop,
            "child_asin": asin,
            "product_id": product_id,
            "metric_snapshot": snapshot,
            "semantic_run": semantic_run,
            "previous_report": previous,
            "report_version": previous.report_version + 1 if previous else 1,
            "input_versions": {
                "metric_snapshot": str(snapshot.pk) if snapshot else None,
                "semantic_run": str(semantic_run.pk) if semantic_run else None,
                "source_manifest": snapshot.source_manifest
                if snapshot
                else semantic_run.source_manifest
                if semantic_run
                else {},
                "rule_version": snapshot.rule_version if snapshot else None,
                "mapping_versions": snapshot.payload.get("mapping_versions", {}) if snapshot else {},
                "prompt_version": semantic_run.prompt_version if semantic_run else None,
                "taxonomy_version": semantic_run.taxonomy_version if semantic_run else None,
                "model": semantic_run.model_name if semantic_run else None,
            },
            "payload": payload,
            "status": "published",
        },
    )
    if created:
        audit(
            actor,
            shop,
            "report.published",
            report.pk,
            after={
                "report_version": report.report_version,
                "previous_report": str(previous.pk) if previous else None,
                "input_versions": report.input_versions,
            },
            request_id=request_id,
        )
    return report, created
