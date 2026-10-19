"""Version-bound lexical evidence retrieval. Search hits are never prevalence denominators."""

import hashlib
import json
import os
import re
import uuid
from functools import lru_cache
from pathlib import Path

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db.models import F, Q

from insight.metrics.facts import authorize_object, latest_facts, source_manifest
from insight.services import audit

from .indexing import INDEX_VERSION, tokens

RETRIEVAL_VERSION = "evidence-candidates-v9"
ALIAS_VERSION = "feedback-alias-v4"
CONTEXT_CANDIDATE_BUDGET = 1000
ALIASES = (
    ("过热", "发烫", "overheat", "overheating", "overheated", "hot", "burning"),
    ("充电慢", "慢充", "slow charge", "charges slowly", "slow charging"),
    ("失效", "无法工作", "stopped working", "quit working", "died"),
    ("包装", "包材", "包装箱", "package", "packaging", "box"),
    ("电气故障", "电气问题", "电器故障", "electrical fault", "electrical failure", "sparks", "power failure"),
    ("受损", "损坏", "damaged", "damage", "broken"),
    ("订单", "order", "order line"),
    ("兼容", "适配", "fit", "compatible", "compatibility", "connector"),
    ("评分", "星级", "rating", "stars", "rating snapshot", "page snapshot"),
    ("退货", "return", "returns"),
    ("签收", "收到", "收货", "到货", "received", "arrived", "warehouse scan"),
    ("发货", "待处理", "shipped", "shipment", "pending"),
    ("删除差评", "删除", "remove", "delete", "negative review"),
    ("销量", "销售额", "sales", "ordered units"),
    ("流量", "sessions", "traffic"),
    ("转化率", "转化", "conversion", "conversion rate"),
    ("退款", "refund", "refunded"),
    ("冲正", "reversal", "reversed"),
    ("假设", "可能", "could", "might", "hypothetical"),
)
SEARCH_STOPWORDS = {
    "find", "look", "search", "recent", "latest", "evidence", "record", "records", "report", "reports",
    "query", "which", "what", "where", "when", "whether", "support", "note", "data", "status",
    "查找", "查一下", "找一下", "记录", "报告", "状态", "证据", "最近", "查询", "是否", "这条", "这个",
}
SEARCH_STOPWORDS |= {
    "the", "a", "an", "and", "or", "for", "of", "to", "in", "on", "this", "that", "it", "is", "was",
    "are", "be", "has", "had", "did", "does", "any", "only", "ask", "will", "would", "can", "please",
    "show", "give", "about", "check", "inspect", "investigate", "confirmed", "customer", "feedback",
    "device", "problem", "issue", "review", "reviews", "case", "cases", "ticket", "tickets", "summary",
    "summarize", "overview", "support", "adapter", "accessory", "complete", "window", "lower",
    "product", "asin", "sku", "new", "evidence", "related", "records", "record",
    "why", "how", "tell", "me", "analyze", "diagnose", "cause", "causes", "reason", "reasons", "explain", "investigation",
    "调查", "分析", "原因", "能", "能否", "需要", "确认", "支持", "请查", "明确", "判断", "检索",
    "请", "帮我", "有", "没有", "有没有", "的", "了", "时", "是", "还是", "在", "已经", "该",
    "查", "查找", "核查", "核对", "到", "是否", "总结", "概括", "梳理", "相关", "有关", "原文",
    "商品", "产品", "设备", "问题", "客户", "买家", "评论", "客服", "工单", "售后", "记录",
    "级", "链", "关联", "配件", "我", "哪些", "请问", "一下", "event", "events",
}
SEARCH_STOPWORDS |= {"与", "已", "仍", "只是", "回升", "增加", "检查", "中", "条", "这",
                     "change", "changed", "listing", "reduce", "reduced", "increase", "increased", "asserts", "asserted"}


def contains_term(text, term):
    return bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I)) if re.fullmatch(r"[a-z0-9 ]+", term) else term in text.lower()


def lexical_query_intent(query):
    """Separate bounded presentation requests and explicit property negation from facts."""
    content = re.sub(r"\s+(?:with|for)\s+(?:additional|more)\s+(?:context|details?|information)\s*[.!?]*$", "", query, flags=re.I)
    content = re.sub(r"[，,]?\s*并(?:补充|提供)(?:背景信息|背景说明|更多细节)[。！？]*$", "", content)
    properties = [group for group in (ALIASES[index] for index in (0, 1, 2, 5))
                  if any(contains_term(content, term) for term in group)]
    inferred = "any"
    if len(properties) == 1:
        # Only bind an immediately preceding operator to the single disputed
        # property. Unknown nouns, causal qualifiers and multi-property logic stay.
        for term in sorted(properties[0], key=len, reverse=True):
            if re.fullmatch(r"[a-z0-9 ]+", term):
                topic_prefix = "|".join(re.escape(word) for word in ALIASES[3] if re.fullmatch(r"[a-z0-9 ]+", word))
                pattern = r"(?<!\w)(?:no|without|never)\s+((?:(?:" + topic_prefix + r")\s+)?" + re.escape(term) + r")(?!\w)"
            else:
                pattern = r"(?:没有|并未|从未)(" + re.escape(term) + r")"
            content, count = re.subn(pattern, r"\1", content, flags=re.I)
            if count:
                inferred = "negated"
    return {"content_query": content, "inferred_evidence_context": inferred,
            "policy": "retain_unknown_and_causal_constraints"}


def query_groups(query):
    """AND distinct concepts; OR synonyms within each concept. Never use gold metadata."""
    query = lexical_query_intent(query)["content_query"]
    groups = [group for group in ALIASES if any(contains_term(query, term) for term in group)]
    remainder = query.lower()
    for group in groups:
        for term in sorted(group, key=len, reverse=True):
            if re.fullmatch(r"[a-z0-9 ]+", term):
                remainder = re.sub(r"(?<!\w)" + re.escape(term) + r"(?!\w)", " ", remainder)
            else:
                remainder = remainder.replace(term, " ")
    terms = [term for term in tokens(remainder) if term not in SEARCH_STOPWORDS and not re.fullmatch(r"[a-z0-9]{10}", term)]
    return groups + [(term,) for term in terms]


def context_anchor_groups(query):
    """Context is attached to the disputed property, not neutral topic nouns."""
    groups = query_groups(query)
    # These are existing lexical concepts, not answers or evaluation-case aliases.
    properties = [ALIASES[index] for index in (0, 1, 2, 5)]
    selected = [group for group in properties if group in groups]
    return selected or groups


def group_filter(group, fields):
    predicate = Q(pk__in=[])
    for term in group:
        for field in fields:
            if re.fullmatch(r"[a-z0-9 ]+", term):
                predicate |= Q(**{f"{field}__iregex": r"(^|[^[:alnum:]_])" + re.escape(term) + r"([^[:alnum:]_]|$)"})
            else:
                predicate |= Q(**{f"{field}__icontains": term})
        predicate |= Q(search_vector=SearchQuery(term, config="english" if re.fullmatch(r"[a-z0-9 ]+", term) else "simple"))
    return predicate


@lru_cache(maxsize=1)
def algorithm_fingerprint():
    return {
        "algorithm_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "alias_sha256": hashlib.sha256(json.dumps(ALIASES, ensure_ascii=False).encode()).hexdigest(),
        "index_algorithm_sha256": hashlib.sha256(Path(__file__).with_name("indexing.py").read_bytes()).hexdigest(),
        "index_rebuild_required": False,
    }


def retrieve(
    actor, shop, asin, *, query, start, end, product_id=None, kind="reviews", limit=10, manifest=None, language=None, topic=None, polarity=None, evidence_context="any", budget_run=None
):
    authorize_object(actor, shop, asin, product_id)
    manifest = manifest if manifest is not None else source_manifest(shop)
    rows = latest_facts(manifest, kind, asin, product_id).filter(business_date__range=(start, end))
    from rest_framework.exceptions import ValidationError

    from insight.models import SemanticRun
    if kind != "reviews" and any((language, topic, polarity)):
        raise ValidationError("语言、主题与极性筛选仅适用于评论。")
    if language:
        rows = rows.filter(language=language)
    if topic or polarity:
        run = SemanticRun.objects.filter(shop=shop, child_asin=asin, product_id=product_id, source_manifest=manifest, status__in=["completed", "partial"]).order_by("-created_at").first()
        if not run:
            rows = rows.none()
        else:
            claims = run.claims.filter(prediction_status="accepted")
            if topic:
                claims = claims.filter(Q(primary_topic=topic) | Q(secondary_topic=topic))
            if polarity:
                claims = claims.filter(polarity=polarity)
            rows = rows.filter(pk__in=claims.values("review_id"))
    fields = ["text"]
    if kind == "reviews":
        rows = rows.filter(is_deleted=False)
        fields.insert(0, "title")
    # Token and alias indexes supplement literal phrases; original text remains evidence.
    from .relevance import enabled
    relevance_enabled = enabled()
    groups = query_groups(query)
    terms = {term for group in groups for term in group}
    predicate = Q()
    candidate_groups = [group for group in groups if group in ALIASES] if relevance_enabled else groups
    candidate_groups = candidate_groups or groups
    if relevance_enabled:
        # Broad lexical candidates are not evidence until the constraint adapter accepts them.
        predicate = Q(pk__in=[])
        for group in candidate_groups:
            predicate |= group_filter(group, fields)
    else:
        for group in candidate_groups:
            predicate &= group_filter(group, fields)
    vector = SearchVector(*fields, config="english")
    search = SearchQuery(query, config="english", search_type="plain")
    chinese = SearchQuery(query, config="simple", search_type="plain")
    for term in sorted(terms):
        chinese |= SearchQuery(term, config="simple", search_type="plain")
        if re.fullmatch(r"[a-z0-9 ]+", term):
            chinese |= SearchQuery(term, config="english", search_type="plain")
    ranked = rows.annotate(rank=SearchRank(F("search_vector"), chinese) + SearchRank(vector, search))
    matched = ranked.filter(predicate) if groups else ranked.none()
    if evidence_context not in {"any", "asserted", "negated", "hypothetical"}:
        raise ValidationError("无效的取证语境。")
    if evidence_context == "any":
        evidence_context = lexical_query_intent(query)["inferred_evidence_context"]
    if evidence_context == "any" and re.search(r"\b(confirmed|asserts|asserted)\b|明确确认", query, re.I):
        evidence_context = "asserted"
    from .safety_context import record_contexts

    context_terms = {term for group in context_anchor_groups(query) for term in group}
    hits, scanned, context_filtered = [], 0, 0
    scoped_count, matched_count = rows.count(), matched.count()
    lexical_matched_count = matched_count
    collection_limit = min(30, max(1, int(os.getenv("EVIDENCE_RELEVANCE_MAX_CANDIDATES", "20")))) if relevance_enabled else limit
    mode = os.getenv("EVIDENCE_CANDIDATE_MODE", "lexical")
    if mode not in {"lexical", "hybrid_local"} or mode == "hybrid_local" and not relevance_enabled:
        raise ValidationError("本地语义候选需要启用受控相关性判定。")
    semantic_metadata = {"status": "disabled"}
    fused_ids = None
    if mode == "hybrid_local" and groups:
        from .vector_candidates import RANK_WINDOW, fuse_candidates, index_scope, vector_candidates
        try:
            semantic_ids, semantic_metadata = vector_candidates(rows, index_scope(shop, asin, product_id, kind, manifest),
                                                               query=query, start=start, end=end)
            lexical_ids = [str(pk) for pk in matched.order_by("-rank", "-business_date", "id").values_list("pk", flat=True)[:RANK_WINDOW]]
            fused_ids, _ = fuse_candidates(lexical_ids, semantic_ids, collection_limit)
            semantic_metadata.update(lexical_ranked_ids=lexical_ids, semantic_ranked_ids=semantic_ids,
                                     fused_ids=fused_ids, fusion="rrf60_semantic_half_reserved_v1")
        except Exception as exc:
            # Missing or stale indexes never silently become successful lexical retrieval.
            semantic_metadata = {"status": "unsupported", "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}
            fused_ids = []
        matched = ranked.filter(pk__in=fused_ids)
        matched_count = len(fused_ids)
    small_scope_adjudication = relevance_enabled and bool(groups) and scoped_count <= collection_limit
    if small_scope_adjudication and mode == "lexical":
        # A bounded authorized scope can be examined in full. Lexical mismatch
        # is not proof of irrelevance; only the constraint adapter admits evidence.
        matched, matched_count = ranked, scoped_count
    retrieval_complete = semantic_metadata["status"] != "unsupported"
    candidates = matched.order_by("-rank", "-business_date", "id")
    if fused_ids:
        from django.db.models import Case, IntegerField, When
        candidates = matched.order_by(Case(*[When(pk=pk, then=position) for position, pk in enumerate(fused_ids)], output_field=IntegerField()))
    budget, chunk = CONTEXT_CANDIDATE_BUDGET, 100
    while len(hits) < collection_limit:
        batch = list(candidates[scanned:scanned + min(chunk, budget - scanned + 1)])
        if not batch:
            break
        for row in batch:
            if scanned >= budget:
                retrieval_complete = False
                break
            scanned += 1
            row.evidence_contexts = record_contexts(title=getattr(row, "title", ""), text=row.text, terms=sorted(context_terms))
            if mode == "hybrid_local" or evidence_context == "any" or any(item["context"] == evidence_context for item in row.evidence_contexts):
                # Synonymous properties may have no lexical anchor. The semantic
                # adapter must apply the explicit context constraint to originals.
                hits.append(row)
            else:
                context_filtered += 1
            if len(hits) == collection_limit:
                break
        if len(hits) == collection_limit or not retrieval_complete or len(batch) < chunk:
            break
    relevance_judgment = {"status": "disabled", "enabled": False}
    adjudication_input_count = len(hits) if relevance_enabled else 0
    if relevance_enabled:
        from insight.investigation.citations import sha

        from .relevance import configured_adapter
        scope = {"shop": str(shop.pk), "asin": asin, "product": str(product_id) if product_id else None,
                 "source_manifest_sha256": sha(manifest), "start": str(start), "end": str(end), "kind": kind,
                 "language": language, "topic": topic, "polarity": polarity, "evidence_context": evidence_context}
        candidate_complete = retrieval_complete and scanned >= matched_count
        if mode == "hybrid_local":
            candidate_complete = candidate_complete and len(fused_ids or []) == scoped_count
        try:
            relevance_judgment = configured_adapter().judge(query=query, candidates=[{"fact_id": str(row.pk), "title": getattr(row, "title", ""), "text": row.text} for row in hits],
                                                            scope=scope, complete=candidate_complete, budget_run=budget_run,
                                                            workspace=shop.workspace)
        except Exception as exc:
            relevance_judgment = {"status": "incomplete", "complete": False, "selected_ids": [], "error": type(exc).__name__, "cost_known": False}
        relevance_judgment["enabled"] = True
        retrieval_complete = retrieval_complete and relevance_judgment["complete"]
        selected = set(relevance_judgment.get("selected_ids", []))
        hits = [row for row in hits if str(row.pk) in selected][:limit]
    scoped_context_fallback = False
    summary_request = bool(re.search(r"\b(summarize|summary|overview)\b|总结|概括|梳理", query, re.IGNORECASE))
    if not hits and summary_request and not groups and evidence_context == "any":
        hits = list(rows.order_by("-business_date", "record_id")[:limit])
        for row in hits:
            row.rank = 0.0
        scoped_context_fallback = bool(hits)
    payload = {
        "retrieval_id": str(uuid.uuid4()),
        "retrieval_version": RETRIEVAL_VERSION,
        "alias_version": ALIAS_VERSION,
        "index_version": INDEX_VERSION,
        "algorithm_manifest": {"relevance_algorithm_sha256": hashlib.sha256(Path(__file__).with_name("relevance.py").read_bytes()).hexdigest(), "candidate_mode": mode,
                               "vector_algorithm_sha256": hashlib.sha256(Path(__file__).with_name("vector_candidates.py").read_bytes()).hexdigest(),
                               "relevance_enabled": relevance_enabled, **algorithm_fingerprint(), "source_manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True, default=str).encode()).hexdigest()},
        "relevance_judgment": relevance_judgment,
        "retrieval_complete": retrieval_complete,
        "candidate_count": scanned,
        "candidate_stage": {"policy": "bounded_hybrid_with_constraint_adjudication" if mode == "hybrid_local" else "bounded_scope_with_constraint_adjudication" if small_scope_adjudication else "bounded_union_with_constraint_adjudication" if relevance_enabled else "strict_conjunction",
                            "query_groups": [list(group) for group in groups], "matched_count": matched_count,
                            "lexical_matched_count": lexical_matched_count,
                            "scoped_count": scoped_count, "lexical_not_admitted_count": scoped_count - lexical_matched_count,
                            "semantic_candidates": semantic_metadata,
                            "scanned_count": scanned, "context_filtered_count": context_filtered,
                            "rank_or_scan_budget_unexamined_count": max(0, matched_count - scanned),
                            "collection_limit": collection_limit, "scan_budget": budget,
                            "adjudication_input_count": adjudication_input_count,
                            "adjudication_decision_count": len(relevance_judgment.get("decisions", [])),
                            "relevance_rejected_count": sum(d.get("label") == "irrelevant" for d in relevance_judgment.get("decisions", [])),
                            "relevance_unknown_count": sum(d.get("label") == "unknown" for d in relevance_judgment.get("decisions", [])),
                            "adjudication_error": relevance_judgment.get("error"),
                            "limitation": "Candidate-stage counts are not measured semantic recall; unexamined originals cannot establish absence."},
        "evidence_context": evidence_context,
        "match_status": "incomplete" if not retrieval_complete and not hits else "scope_context" if scoped_context_fallback else "relevant_hits" if hits else "no_hits",
        "relevant_hit_count": 0 if scoped_context_fallback else len(hits),
        "context_item_count": len(hits) if scoped_context_fallback else 0,
        "source_manifest": manifest,
        "scope": {
            "shop": str(shop.pk),
            "asin": asin,
            "product": str(product_id) if product_id else None,
            "start": str(start),
            "end": str(end),
            "kind": kind, "language": language, "topic": topic, "polarity": polarity,
        },
        "query": query,
        "query_intent": lexical_query_intent(query),
        "items": [
            {
                "fact_id": str(row.pk),
                "record_id": row.record_id,
                "raw_row_id": str(row.raw_row_id),
                "revision": row.revision,
                "business_date": str(row.business_date),
                "title": getattr(row, "title", ""),
                "text": row.text,
                "rank": float(row.rank),
                "match_kind": "scope_context" if scoped_context_fallback else "relevant_hit",
                "evidence_relation": "unknown",
                "evidence_contexts": getattr(row, "evidence_contexts", []),
            }
            for row in hits
        ],
        "limitations": [
            "检索命中是取证样本，不能用于计算主题发生率或证明因果。",
            "当前采用有界词法和本地向量候选，再做约束判定；真实运营语料召回率待验收。" if mode == "hybrid_local" else "当前使用中文分词、英文词干、短语与别名检索；未启用向量检索，真实运营语料召回率待验收。",
        ] + (["候选扫描或语义判定未完成，不能据此断言没有证据。"] if not retrieval_complete else []) + (
            ["词法查询没有主题命中；所列内容仅为同一授权范围和日期内的近期原文，需人工确认相关性。"]
            if scoped_context_fallback
            else []
        ),
    }
    audit(actor, shop, "evidence.retrieved", payload["retrieval_id"], after=payload)
    return payload
