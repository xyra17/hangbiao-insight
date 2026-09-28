"""Deterministic S4 diagnostic assembly: facts, scenarios, coverage and safety routing."""

from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from insight.ingestion.contracts import digest
from insight.metrics.facts import latest_facts
from insight.models import ActionTask, BusinessEvent, EventEvidence, Notification

SCENARIOS = {
    "sales": (
        "销量下降",
        ["数据覆盖", "可售及购物车", "库存", "Sessions", "件数会话比", "运营变化", "体验反馈"],
    ),
    "conversion": ("转化下降", ["分子分母", "子体组合", "购物车及库存", "价格促销", "页面及反馈"]),
    "refunds": (
        "退货退款增加",
        ["发生量与成熟队列", "原因数量占比", "子体及履约", "处置和工单", "批次及运营变更"],
    ),
    "ratings": ("评分或评论变化", ["页面评分及采集完整性", "变体归属", "主题极性及样本", "退款工单对照"]),
    "safety": ("严重安全反馈", ["原文及否定语境", "对象匹配", "立即人工核查"]),
}


def _fact(metric, source, value, *, support=True, confidence="observed", refs=None):
    return {
        "fact_id": digest({"metric": metric, "source": source, "value": value, "refs": refs or []}),
        "metric": metric,
        "source": source,
        "value": value,
        "support": support,
        "confidence": confidence,
        "source_refs": refs or [],
    }


def _manifest_is_resolvable(manifest):
    """Legacy snapshots may carry opaque manifests; never let them masquerade as facts."""
    return isinstance(manifest, dict) and all(
        isinstance(value, dict) and "kind" in value and "revision" in value
        for value in manifest.values()
    )


def _metric_statistics(section, payload, signal):
    """Expose the frozen statistical contract without inventing unavailable values."""
    baseline = payload.get(section, {}).get("baseline", {})
    current = payload.get(section, {}).get("current", {})
    metric = signal.get("metric")
    pairs = {
        "sales_units": ("ordered_units", "units"),
        "sessions": ("sessions", "sessions"),
        "unit_session_percent": ("unit_session_percent", "percent"),
        "buybox": ("buybox_percent", "percent"),
        "returns": ("returns", "records"),
        "refunds": ("refunds", "records"),
    }
    field, unit = pairs.get(metric, (None, None))
    base = baseline.get(field) if field else None
    now = current.get(field) if field else None
    delta = None
    if base is not None and now is not None:
        try:
            delta = str(float(now) - float(base))
        except (TypeError, ValueError):
            delta = None
    numerator = current.get("ordered_units") if metric == "unit_session_percent" else now
    denominator = current.get("sessions") if metric == "unit_session_percent" else None
    return {
        "numerator": numerator,
        "denominator": denominator,
        "unit": unit,
        "baseline": base,
        "current": now,
        "delta": delta,
        "sample": current.get("days") or current.get("analyzed"),
        "sample_unit": "days" if current.get("days") is not None else "records",
        "confidence_interval": signal.get("confidence_interval"),
        "method": signal.get("method") or "deterministic_rule",
    }


def build_diagnostic_payload(snapshot, semantic_run=None):
    from zoneinfo import ZoneInfo

    from .trends import topic_trends

    material = snapshot or semantic_run
    as_of = (
        snapshot.local_date
        if snapshot
        else semantic_run.created_at.astimezone(ZoneInfo(semantic_run.shop.timezone)).date()
        if semantic_run
        else timezone.now().date()
    )
    trends = topic_trends(semantic_run, as_of)
    payload = snapshot.payload if snapshot else {}
    start = date.fromisoformat(payload.get("daily", {}).get("baseline", {}).get("start", str(as_of - timedelta(days=56))))
    end = as_of - timedelta(days=1)
    facts, scenarios = [], {}
    # Keep the linkage result as a first-class, auditable fact.  Previously it
    # was only returned at the top level, which made it possible for a report
    # consumer to miss the difference between one linked order experience and
    # several unlinked source rows.  The linkage is still descriptive: it
    # never changes metric denominators or turns a text match into an order
    # identity.
    experience = None
    for section in ("daily", "absolute", "cohort", "occurrence"):
        data = payload.get(section, {})
        for signal in data.get("signals", []):
            item = _fact(
                    signal.get("metric", section),
                    "metric_snapshot",
                    signal,
                    support=True,
                    refs=[str(snapshot.pk)],
                )
            item["statistics"] = _metric_statistics(section, payload, signal)
            facts.append(item)
    # Cross-source facts are linked by stable source references; they never increase review denominators.
    if snapshot and _manifest_is_resolvable(snapshot.source_manifest):
        from .experiences import experience_links

        experience = experience_links(snapshot, start, end)
        linkage_refs = sorted({
            str(ref.get("raw_row_id"))
            for group in experience.get("groups", [])
            for ref in group.get("records", [])
            if ref.get("raw_row_id")
        } | {
            str(ref.get("raw_row_id"))
            for ref in experience.get("unlinked", [])
            if ref.get("raw_row_id")
        })
        if linkage_refs:
            facts.append(_fact(
                "experience_linkage",
                "experience_links",
                {
                    "status": experience.get("status"),
                    "linked_order_count": experience.get("linked_order_count", 0),
                    "source_counts": experience.get("source_counts", {}),
                    "unlinked_count": len(experience.get("unlinked", [])),
                },
                confidence="linked_order_identity" if experience.get("groups") else "unlinked",
                refs=linkage_refs,
            ))
        for kind in ("support", "operations"):
            for row in latest_facts(
                snapshot.source_manifest, kind, snapshot.child_asin, snapshot.product_id
            ).filter(source__shop_id=snapshot.shop_id, business_date__range=(start, end)):
                value = {"record_id": row.record_id, "business_date": str(row.business_date)}
                if kind == "operations":
                    value.update({
                        "event_type": row.event_type,
                        "ends_on": str(row.ends_on) if row.ends_on else None,
                        "price": str(row.price) if row.price is not None else None,
                        "currency": row.currency,
                        "mechanism": row.mechanism,
                    })
                elif kind == "support":
                    value.update({"order_line_id": row.order_line_id, "status": row.status})
                facts.append(
                    _fact(
                        kind,
                        kind,
                        value,
                        refs=[str(row.raw_row_id)],
                    )
                )
    claims = []
    if semantic_run:
        valid_reviews = latest_facts(semantic_run.source_manifest, "reviews", semantic_run.child_asin, semantic_run.product_id).filter(
            source__shop_id=semantic_run.shop_id, is_deleted=False, business_date__range=(start, end)
        )
        for claim in semantic_run.claims.filter(prediction_status="accepted", review__in=valid_reviews).select_related("review"):
            item = _fact(
                claim.secondary_topic,
                "semantic_claim",
                {"polarity": claim.polarity, "span": claim.evidence_span},
                support=claim.polarity in {"negative", "mixed"},
                confidence="accepted",
                refs=[str(claim.review.raw_row_id)],
            )
            item.update(
                {
                    "claim_id": str(claim.pk),
                    "primary_topic": claim.primary_topic,
                    "polarity": claim.polarity,
                    "review_date": str(claim.review.business_date),
                }
            )
            claims.append(item)
            facts.append(item)
    for fact in facts:
        text = fact["source"] == "semantic_claim"
        fact.update(
            {
                "source_type": fact["source"],
                "fact_type": "text" if text else "metric_signal",
                "analysis_scope": {
                    "shop": str(material.shop_id),
                    "child_asin": material.child_asin,
                    "product": str(material.product_id) if material.product_id else None,
                },
                "native_grain": "review_record" if text else "see_metric_snapshot",
                "window_start": fact.get("review_date") if text else (
                    fact["value"].get("start") or payload.get("daily", {}).get("baseline", {}).get("start")
                ),
                "window_end": fact.get("review_date") if text else (
                    fact["value"].get("end") or fact["value"].get("cutoff")
                ),
                "data_as_of": str(as_of),
                "coverage_status": "sample_only" if text else "see_metric_snapshot",
                "data_version": semantic_run.source_manifest if text else snapshot.source_manifest,
                "rule_version": semantic_run.prompt_version if text else snapshot.rule_version,
                "material_ref": str(semantic_run.pk if text else snapshot.pk),
            }
        )
        if not text:
            fact.setdefault("state", {"occurred_at": None, "before": None, "after": None, "event_status": None})
        else:
            fact.setdefault("text_evidence", {
                "record_id": fact.get("claim_id"), "span": fact.get("value", {}).get("span"),
                "offset_start": None, "offset_end": None, "language": None,
                "topic": fact.get("primary_topic"), "polarity": fact.get("polarity"),
                "classifier_version": fact.get("rule_version"), "human_review": False,
            })
    # Explicit unmet checks prevent a single signal from completing a scenario.
    checks = {
        "数据覆盖": payload.get("daily", {}).get("status") == "ready",
        "可售及购物车": payload.get("absolute", {}).get("inventory", {}).get("status") == "ready",
        "库存": payload.get("absolute", {}).get("inventory", {}).get("available") is not None,
        "Sessions": payload.get("daily", {}).get("current", {}).get("sessions") is not None,
        "件数会话比": payload.get("daily", {}).get("current", {}).get("ratio_status") == "ready",
        "运营变化": bool(payload.get("comparability", {}).get("operations")),
        "体验反馈": bool(claims),
        "页面及反馈": bool(claims),
        "发生量与成熟队列": all(
            payload.get("cohort", {}).get(k, {}).get("status") == "ready" for k in ("returns", "refunds")
        ),
        "主题极性及样本": trends["status"] == "ready",
        "原文及否定语境": bool(claims),
        "对象匹配": bool(semantic_run),
    }
    for key, (label, required) in SCENARIOS.items():
        gaps = [name for name in required if not checks.get(name, False)]
        scenarios[key] = {
            "label": label,
            "status": "partial" if gaps else "ready",
            "required_checks": required,
            "checks": [
                {"name": name, "status": "checked" if checks.get(name, False) else "missing"}
                for name in required
            ],
            "gaps": gaps,
        }
    candidates = []
    directions = {
        "availability": (
            "可售或库存阻碍",
            [f for f in facts if f["metric"] == "availability"],
            "库存快照不能证明精确断货小时数",
        ),
        "traffic": (
            "购物车或流量变化",
            [f for f in facts if f["metric"] in {"sessions", "buybox"}],
            "不能据此判断广告、排名或竞品原因",
        ),
        "fit": (
            "描述或适配问题",
            [
                f
                for f in claims
                if f.get("primary_topic") == "compatibility_fit" and f["polarity"] in {"negative", "mixed"}
            ],
            "需核查页面和规格；不合适不直接证明描述错误",
        ),
        "price": (
            "价格促销变化",
            [
                f for f in facts
                if f["source"] == "operations"
                and f.get("value", {}).get("record_id")
                and f.get("value", {}).get("event_type") in {"price", "promotion", "coupon", "deal"}
                and (f["value"].get("price") is not None or f["value"].get("mechanism"))
                and any(m["source"] == "metric_snapshot" and m["metric"] in {"sales_units", "unit_session_percent"} for m in facts)
            ],
            "到手价、促销归因和客户构成仍需核查",
        ),
        "variant_mix": (
            "子体组合变化",
            [
                f
                for f in facts
                if f["metric"] in {"sales_units", "returns", "refunds"} and f["source"] == "metric_snapshot"
            ] if any(
                isinstance(value, dict) and value.get("status") == "ready"
                for value in payload.get("decomposition", {}).values()
            ) else [],
            "缺少可比子体分层和销售权重时不计算分解值",
        ),
        "product": (
            "产品或包装问题",
            [
                f
                for f in claims
                if f.get("primary_topic")
                in {"reliability_durability", "function_performance", "packaging", "safety_risk"}
                and f["polarity"] in {"negative", "mixed"}
            ],
            "缺陷、批次及跨源经历关联仍需人工核验",
        ),
    }
    for key, (label, supporting, limitation) in directions.items():
        if not supporting:
            continue
        refs = sorted({r for f in supporting for r in f["source_refs"]})
        counter = [
            f["fact_id"]
            for f in claims
            if f["polarity"] == "positive"
            and f.get("primary_topic") in {s.get("primary_topic") for s in supporting}
        ]
        # A candidate is an investigation lead only.  Keep this explicit in
        # the payload so the UI and downstream exports cannot mistake a
        # ranked rule signal for an established causal explanation.  In
        # particular, a missing Sessions denominator must remain visible as a
        # hard limitation for traffic/conversion interpretation.
        candidate_missing = [limitation]
        if key in {"traffic", "price", "variant_mix"} and not checks.get("Sessions", False):
            candidate_missing.append("缺少完整 Sessions 分母，禁止据此确定销量或转化因果")
        candidates.append(
            {
                "candidate_id": key,
                "title": label,
                "status": "needs_verification",
                "support_facts": [f["fact_id"] for f in supporting],
                "support_refs": refs,
                "counter_evidence": counter,
                "conflicting_facts": [],
                "missing_checks": candidate_missing,
                "next_action": "回查原文、原始报表及商品实物并记录人工核查结论",
                "limitations": candidate_missing,
                "causal_status": "not_established",
                "causal_claim_allowed": False,
                "ranking": {
                    "human_verified": False,
                    "object_match": True,
                    "time_match": False,
                    "evidence_complete": False,
                    "independent_sources": "unknown",
                    "unresolved_counter_count": len(counter),
                },
            }
        )
    candidates.sort(key=lambda c: (c["ranking"]["unresolved_counter_count"], c["candidate_id"]))
    coverage = {
        "gate": trends["status"],
        "windows": trends["windows"],
        "effective_denominator": [
            {"start": window.get("start"), "end": window.get("end"), "analyzed": window.get("analyzed", 0)}
            for window in trends.get("windows", [])
        ],
        "gate_reason": (
            "两个完整窗口均达到最小样本、覆盖率和来源完整性"
            if trends.get("status") == "ready"
            else "样本、覆盖率或来源完整性不足，禁止把主题趋势当作总体结论"
        ),
    }
    trend = trends
    safety = [
        i for i in claims if i.get("primary_topic") == "safety_risk" and i.get("polarity") == "negative"
    ]
    from .experiences import experience_links

    return {
        "experience_links": experience if experience is not None else (
            experience_links(material, start, end) if material else None
        ),
        "evidence_facts": facts,
        "scenarios": scenarios,
        "candidates": candidates,
        "coverage": coverage,
        "topic_trends": trend,
        "safety": {
            "urgent": bool(safety),
            "claims": safety,
            "route": "immediate_review" if safety else "normal_review",
        },
        "limitations": ["候选原因按证据样本排序；未知数据不会被解释为没有风险。"],
    }


@transaction.atomic
def route_safety_event(actor, shop, asin, product_id, semantic_run, request_id=""):
    from django.contrib.auth import get_user_model
    from rest_framework.exceptions import PermissionDenied

    from insight.metrics.facts import authorize_object
    from insight.metrics.service import scan_object
    from insight.models import Shop
    from insight.services import audit

    Shop.objects.select_for_update().get(pk=shop.pk)
    authorize_object(actor, shop, asin, product_id)
    claims = list(
        semantic_run.claims.filter(
            primary_topic="safety_risk", polarity="negative", prediction_status="accepted"
        ).select_related("review")
    )
    if not claims:
        return None
    snapshot = scan_object(actor, shop, asin, product_id, request_id=request_id)
    if snapshot.source_manifest != semantic_run.source_manifest:
        return None
    object_key = str(product_id) if product_id else f"asin:{asin}"
    event = (
        BusinessEvent.objects.filter(shop=shop, object_key=object_key, family="safety")
        .exclude(status="closed")
        .first()
    )
    reopened_from = None
    if not event:
        closed = (
            BusinessEvent.objects.filter(shop=shop, object_key=object_key, family="safety", status="closed")
            .filter(closed_at__gte=timezone.now() - timedelta(days=30))
            .order_by("-closed_at")
            .first()
        )
        reopened_from = closed
    # Evidence identity survives re-analysis; repeating the same source cannot notify again.
    refs = sorted({str(c.review.raw_row_id) for c in claims})
    previous_refs = {
        r
        for detail in EventEvidence.objects.filter(
            event__shop=shop, event__object_key=object_key, metric="safety_feedback"
        ).values_list("detail", flat=True)
        for r in detail.get("raw_row_ids", [])
    }
    if not set(refs) - previous_refs:
        return event
    if not event:
        event = BusinessEvent.objects.create(
            shop=shop,
            child_asin=asin,
            product_id=product_id,
            object_key=object_key,
            family="safety",
            title="反馈存在安全线索，请立即人工核查",
            priority=0,
            last_cutoff=snapshot.local_date,
            last_seen=timezone.now(),
            reopened_from=reopened_from,
        )
    event.priority, event.snoozed_until, event.snooze_reason = 0, None, ""
    event.version += 1
    event.last_seen = timezone.now()
    event.save()
    evidence, _ = EventEvidence.objects.get_or_create(
        event=event, snapshot=snapshot, metric="safety_feedback", defaults={"detail": {}}
    )
    old_detail = evidence.detail or {}
    refs = sorted(set(refs) | set(old_detail.get("raw_row_ids", [])))
    claim_ids = sorted(set(old_detail.get("claim_ids", [])) | {str(c.pk) for c in claims})
    evidence.detail = {
        "semantic_run": str(semantic_run.pk),
        "claim_ids": claim_ids,
        "raw_row_ids": refs,
    }
    evidence.save(update_fields=["detail"])
    # Safety feedback always produces an internal, assigned human-check task.
    if not ActionTask.objects.filter(event=event, status__in=["open", "in_progress"]).exists():
        assignee = event.owner
        if assignee is None:
            assignee = (
                get_user_model().objects.filter(
                    is_active=True, membership__workspace=shop.workspace, membership__is_admin=True
                ).order_by("id").first()
            )
        ActionTask.objects.create(
            shop=shop,
            event=event,
            child_asin=asin,
            product_id=product_id,
            title="安全反馈立即人工核查",
            question="核对原文、否定/假设语境、对象归属及是否存在真实安全经历。",
            completion_criteria="记录人工核查结论、证据引用和是否需要升级。",
            evidence_refs=refs,
            assigned_to=assignee,
            created_by=actor,
            action_type="safety",
            is_formal=False,
        )
    for user in (
        get_user_model().objects.filter(is_active=True, membership__workspace=shop.workspace).distinct()
    ):
        try:
            authorize_object(user, shop, asin, product_id)
        except PermissionDenied:
            continue
        Notification.objects.get_or_create(
            user=user, event=event, kind="safety_immediate", data_version=digest(refs)
        )
    audit(
        actor,
        shop,
        "safety.routed",
        event.pk,
        after={"semantic_run": str(semantic_run.pk), "raw_row_ids": refs},
        request_id=request_id,
    )
    return event


def validate_diagnostic_payload(diagnosis, snapshot=None, semantic_run=None):
    """Evidence Guard: reject facts whose references or scope/version disagree."""
    errors = []
    material = snapshot or semantic_run
    expected_scope = (
        {
            "shop": str(material.shop_id),
            "child_asin": material.child_asin,
            "product": str(material.product_id) if material.product_id else None,
        }
        if material
        else None
    )
    facts = diagnosis.get("evidence_facts", [])
    fact_ids = {fact.get("fact_id") for fact in facts if fact.get("fact_id")}
    if len(facts) > 500:
        errors.append("事实数量超过单份诊断上限")
    for fact in facts:
        if not fact.get("fact_id") or not fact.get("source_refs"):
            errors.append("事实缺少稳定编号或来源引用")
        if expected_scope and fact.get("analysis_scope") != expected_scope:
            errors.append("事实对象范围与报告范围不一致")
        if (
            fact.get("source") == "metric_snapshot"
            and snapshot
            and fact.get("material_ref") != str(snapshot.pk)
        ):
            errors.append("指标事实未引用当前快照")
        if (
            fact.get("source") == "semantic_claim"
            and semantic_run
            and fact.get("data_version") != semantic_run.source_manifest
        ):
            errors.append("文本事实未引用当前语义输入版本")
        if len(fact.get("source_refs", [])) != len(set(fact.get("source_refs", []))):
            errors.append("事实来源引用存在重复")
        if fact.get("source") == "semantic_claim" and semantic_run:
            valid_review_refs = {
                str(claim.review.raw_row_id)
                for claim in semantic_run.claims.filter(prediction_status="accepted").select_related("review")
            }
            if not set(fact.get("source_refs", [])) <= valid_review_refs:
                errors.append("文本事实引用不属于当前已接受语义结果")
    fact_refs = {
        str(ref)
        for fact in facts
        for ref in fact.get("source_refs", [])
        if ref is not None
    }
    for candidate in diagnosis.get("candidates", []):
        if not candidate.get("support_facts"):
            errors.append("候选缺少支持事实")
        if candidate.get("status") == "confirmed":
            errors.append("候选不得由规则自动标记为已确认")
        supports = candidate.get("support_facts", [])
        if len(supports) != len(set(supports)) or not set(supports) <= fact_ids:
            errors.append("候选支持事实不存在或重复")
        counters = candidate.get("counter_evidence", [])
        if not set(counters) <= fact_ids:
            errors.append("候选反证事实不存在")
        support_refs = {str(ref) for ref in candidate.get("support_refs", [])}
        counter_refs = {str(ref) for ref in candidate.get("counter_refs", [])}
        if not support_refs <= fact_refs:
            errors.append("候选支持引用不存在于事实来源")
        if not counter_refs <= fact_refs:
            errors.append("候选反证引用不存在于事实来源")
        if candidate.get("causal_status") == "confirmed" or candidate.get("causal_claim_allowed") is True:
            errors.append("规则候选不得声明已确认因果")
        if len(supports) > 100 or len(candidate.get("support_refs", [])) > 100:
            errors.append("候选引用数量超过上限")
    return {"valid": not errors, "errors": sorted(set(errors))}
