from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from insight.ingestion.contracts import digest
from insight.metrics.facts import latest_facts, source_manifest
from insight.models import Review, SemanticClaim, SemanticReviewResult, SemanticRun
from insight.services import audit

TAXONOMY_VERSION = "taxonomy-v1.1"
PROMPT_VERSION = "semantic-prompt-v1.2"
SEMANTIC_VERSION = "review-semantic-v1.0"


@dataclass(frozen=True)
class Claim:
    primary_topic: str
    secondary_topic: str
    polarity: str
    evidence_span: str
    prediction_status: str = "accepted"
    quality_flags: tuple[str, ...] = ()
    raw_response_ref: str = ""


class AdapterError(RuntimeError):
    pass


class DeterministicAdapter:
    """Explicit test and fallback adapter; it never claims model-backed semantics."""

    provider = "deterministic"
    model_name = "keyword-baseline"

    def extract(self, text: str, title: str = "") -> list[Claim]:
        source = f"{title}\n{text}" if title and title not in text else text
        rules = [
            (("safety_risk", "overheating", "negative"), ("overheat", "hot", "burning")),
            (
                ("function_performance", "charging_speed", "negative"),
                ("slow charge", "charges slowly", "slowly"),
            ),
            (
                ("function_performance", "charging_speed", "positive"),
                ("charges fast", "fast charging", "quickly"),
            ),
            (
                ("reliability_durability", "stopped_working", "negative"),
                ("stopped working", "quit working", "died"),
            ),
            (
                ("compatibility_fit", "case_compatibility", "negative"),
                ("does not fit", "doesn't fit", "case"),
            ),
        ]
        claims: list[Claim] = []
        for (primary, secondary, polarity), phrases in rules:
            evidence = next((phrase for phrase in phrases if phrase.lower() in source.lower()), None)
            if evidence:
                start = source.lower().find(evidence.lower())
                prefix = source.lower()[max(0, start - 8) : start]
                if any(token in prefix.split()[-2:] for token in ("not", "no", "without", "没有", "不是")):
                    evidence = None
            if evidence:
                actual = next(
                    (
                        source[i : i + len(evidence)]
                        for i in range(len(source))
                        if source[i : i + len(evidence)].lower() == evidence.lower()
                    ),
                    evidence,
                )
                claims.append(Claim(primary, secondary, polarity, actual))
        return claims


class ProviderAdapter:
    """Adapter around the frozen provider gateway; no SDK calls occur in domain code."""

    def __init__(self, provider: str):
        from django.conf import settings

        from app.semantic.factory import build_semantic_gateway, build_semantic_service
        from app.semantic.schemas import ReviewInput

        taxonomy_path = Path(
            os.getenv(
                "SEMANTIC_TAXONOMY_PATH",
                str(settings.BASE_DIR.parent / "dataset/data/03_黄金评论集/主题分类体系_v1_1.json"),
            )
        )
        if not taxonomy_path.exists():
            raise AdapterError("语义 taxonomy 文件不存在，禁止启动模型取证。")
        gateway = build_semantic_gateway(
            provider,
            runtime_overrides={
                "timeout_seconds": 30,
                "max_attempts": 2,
                "prompt_version": PROMPT_VERSION,
            },
        )
        self.service = build_semantic_service(taxonomy_path, gateway=gateway)
        self.provider = self.service.gateway.config.provider
        self.model_name = self.service.gateway.config.model
        self._review_input = ReviewInput

    def extract(self, text: str, title: str = "") -> list[Claim]:
        review = self._review_input(
            review_id="runtime-review", product_id="runtime-product", title=title, text=text
        )
        guarded = self.service.extract(review)
        return [
            Claim(
                p.primary_topic,
                p.secondary_topic,
                str(p.polarity),
                p.evidence_span,
                str(p.prediction_status),
                tuple(p.quality_flags),
                p.raw_response_ref or "",
            )
            for p in guarded.predictions
        ]


def adapter_for(provider: str | None = None):
    selected = (provider or os.getenv("SEMANTIC_PROVIDER", "disabled")).strip().lower()
    if selected in {"test", "deterministic", "disabled"}:
        return DeterministicAdapter()
    if selected in {"deepseek", "lmstudio", "qwen"}:
        enabled = {value.strip() for value in os.getenv("INSIGHT_ENABLED_MODEL_PROVIDERS", "").split(",")}
        if selected not in enabled:
            raise AdapterError("该模型尚未获部署授权：请先确认服务商、数据区域和预算。")
        return ProviderAdapter(selected)
    raise AdapterError(f"不支持的语义供应商：{selected}。")


def _review_input(row: Review) -> tuple[str, str]:
    return row.text, row.title


def _run_semantic(
    actor, shop, asin, product_id=None, provider="deterministic", request_id="", frozen_manifest=None,
    resume_run=None,
):
    from insight.ingestion.service import require_source_access
    from insight.metrics.facts import authorize_object

    require_source_access(actor, shop)
    authorize_object(actor, shop, asin, product_id)
    manifest = source_manifest(shop) if frozen_manifest is None else frozen_manifest
    rows = list(
        latest_facts(manifest, "reviews", asin, product_id)
        .filter(is_deleted=False)
        .order_by("business_date", "id")
    )
    if resume_run is not None:
        run = SemanticRun.objects.select_for_update().get(pk=resume_run.pk)
        if run.shop_id != shop.pk or run.child_asin != asin or run.product_id != product_id:
            raise ValueError("只能恢复同一店铺、ASIN 和 SKU 范围的语义任务。")
        if run.source_manifest != manifest:
            raise ValueError("来源版本已变化，不能恢复旧语义任务；请重新运行。")
        rows = [row for row in rows if not run.review_results.filter(review_id=row.pk, status="completed").exists()]
        run.status = "running"
        run.finished_at = None
        run.save(update_fields=["status", "finished_at"])
    else:
        run = SemanticRun.objects.create(
            shop=shop,
            child_asin=asin,
            product_id=product_id,
            requested_by=actor,
            status="running",
            provider=provider,
            model_name="uninitialized",
            prompt_version=PROMPT_VERSION,
            taxonomy_version=TAXONOMY_VERSION,
            source_manifest=manifest,
            total_reviews=len(rows),
        )
    try:
        adapter = adapter_for(provider)
    except Exception as exc:
        # A missing key, taxonomy, or unavailable local gateway is a recorded
        # deferred run, rather than an untraceable Celery failure.
        require_source_access(actor, shop)
        authorize_object(actor, shop, asin, product_id)
        run.status = "deferred"
        run.error = (
            str(exc)[:500]
            if isinstance(exc, AdapterError)
            else "模型适配器未能初始化，请核对已批准的部署配置。"
        )
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error", "finished_at"])
        audit(
            actor,
            shop,
            "semantic.deferred",
            run.pk,
            after={"status": run.status, "source_manifest": manifest},
            request_id=request_id,
        )
        return run
    run.provider = adapter.provider
    run.model_name = adapter.model_name
    run.save(update_fields=["provider", "model_name"])
    accepted = rejected = errors = 0
    for row in rows:
        # Recheck before every external call; a queued job's original grant is not authority.
        require_source_access(actor, shop)
        authorize_object(actor, shop, asin, product_id)
        SemanticClaim.objects.filter(run=run, review=row).delete()
        receipt, _ = SemanticReviewResult.objects.update_or_create(
            run=run, review=row, defaults={"status": "running", "detail": ""}
        )
        try:
            claims = adapter.extract(*_review_input(row))
        except Exception:
            errors += 1
            receipt.status = "failed"
            receipt.save(update_fields=["status"])
            continue
        # A revocation while waiting for the provider must stop publication too.
        require_source_access(actor, shop)
        authorize_object(actor, shop, asin, product_id)
        valid = True
        try:
            for index, claim in enumerate(claims, start=1):
                identity = f"{row.record_id}|{index}|{claim.primary_topic}|{claim.secondary_topic}|{claim.polarity}|{claim.evidence_span}"
                prediction_id = f"SP-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
                flags = list(claim.quality_flags)
                if claim.primary_topic == "safety_risk" and claim.polarity in {"negative", "mixed"}:
                    from .safety_context import safety_context
                    context = safety_context(row.title + "\n" + row.text, claim.evidence_span)
                    if context != "asserted":
                        flags.append("safety_context_" + context)
                if claim.evidence_span not in (row.title + "\n" + row.text):
                    flags.append("evidence_not_found")
                status = "rejected" if flags or claim.prediction_status != "accepted" else "accepted"
                SemanticClaim.objects.create(
                    run=run,
                    shop=shop,
                    review=row,
                    product=row.product,
                    prediction_id=prediction_id,
                    primary_topic=claim.primary_topic,
                    secondary_topic=claim.secondary_topic,
                    polarity=claim.polarity,
                    evidence_span=claim.evidence_span,
                    prediction_status=status,
                    quality_flags=flags,
                    provider=adapter.provider,
                    model_name=adapter.model_name,
                    prompt_version=PROMPT_VERSION,
                    taxonomy_version=TAXONOMY_VERSION,
                    raw_response_ref=claim.raw_response_ref,
                )
                if status == "accepted":
                    accepted += 1
                else:
                    rejected += 1
                    valid = False
        except Exception:
            errors += 1
            valid = False
        receipt.status = "completed" if valid else "failed"
        receipt.save(update_fields=["status"])
    require_source_access(actor, shop)
    authorize_object(actor, shop, asin, product_id)
    from .diagnosis import route_safety_event

    run.status = "completed" if errors == 0 else "partial"
    run.accepted_claims, run.rejected_claims, run.error_count = accepted, rejected, errors
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "accepted_claims", "rejected_claims", "error_count", "finished_at"])
    route_safety_event(actor, shop, asin, product_id, run, request_id=request_id)
    audit(
        actor,
        shop,
        "semantic.completed",
        run.pk,
        after={"status": run.status, "source_manifest": manifest, "accepted_claims": accepted},
        request_id=request_id,
    )
    return run


@transaction.atomic
def run_semantic(
    actor, shop, asin, product_id=None, provider="deterministic", request_id="", frozen_manifest=None
):
    return _run_semantic(
        actor, shop, asin, product_id, provider, request_id, frozen_manifest
    )


def resume_semantic_run(actor, run_id, request_id=""):
    """Resume incomplete per-review receipts against the exact frozen source manifest."""
    run = SemanticRun.objects.get(pk=run_id)
    return _run_semantic(
        actor,
        run.shop,
        run.child_asin,
        run.product_id,
        provider=run.provider,
        request_id=request_id,
        frozen_manifest=run.source_manifest,
        resume_run=run,
    )


def report_publication_key(shop, asin, snapshot, semantic_run, product_id=None):
    return digest(
        {
            "shop": str(shop.pk),
            "asin": asin,
            "product": str(product_id) if product_id else None,
            "snapshot": str(snapshot.pk) if snapshot else None,
            "semantic_run": str(semantic_run.pk) if semantic_run else None,
        }
    )
