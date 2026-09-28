"""Version-bound lexical evidence retrieval. Search hits are never prevalence denominators."""

import re
import uuid

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db.models import F, Q

from insight.metrics.facts import authorize_object, latest_facts, source_manifest
from insight.services import audit

from .indexing import INDEX_VERSION, tokens

RETRIEVAL_VERSION = "lexical-evidence-v2"
ALIAS_VERSION = "feedback-alias-v2"
ALIASES = (
    ("过热", "发烫", "overheat", "overheating", "overheated", "hot", "burning"),
    ("充电慢", "慢充", "slow charge", "charges slowly", "slow charging"),
    ("失效", "无法工作", "stopped working", "quit working", "died"),
)


def retrieve(
    actor, shop, asin, *, query, start, end, product_id=None, kind="reviews", limit=10, manifest=None, language=None, topic=None, polarity=None
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
    terms = set(tokens(query))
    terms.add(query.strip().lower())
    for group in ALIASES:
        if any(
            (
                term in query.lower()
                if re.search(r"[\u4e00-\u9fff]", term)
                else re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", query.lower())
            )
            for term in group
        ):
            terms.update(group)
    terms.discard("")
    literal = Q(pk__in=[])
    for term in sorted(terms):
        for field in fields:
            if re.fullmatch(r"[a-z0-9 ]+", term):
                pattern = r"(^|[^[:alnum:]_])" + re.escape(term) + r"([^[:alnum:]_]|$)"
                literal |= Q(**{f"{field}__iregex": pattern})
            else:
                literal |= Q(**{f"{field}__icontains": term})
    vector = SearchVector(*fields, config="english")
    search = SearchQuery(query, config="english", search_type="plain")
    chinese = SearchQuery(query, config="simple", search_type="plain")
    for term in sorted(terms):
        chinese |= SearchQuery(term, config="simple", search_type="plain")
        if re.fullmatch(r"[a-z0-9 ]+", term):
            chinese |= SearchQuery(term, config="english", search_type="plain")
    ranked = rows.annotate(rank=SearchRank(F("search_vector"), chinese) + SearchRank(vector, search))
    matched = ranked.filter(literal | Q(search_vector=chinese))
    hits = list(matched.order_by("-rank", "-business_date", "id")[:limit])
    payload = {
        "retrieval_id": str(uuid.uuid4()),
        "retrieval_version": RETRIEVAL_VERSION,
        "alias_version": ALIAS_VERSION,
        "index_version": INDEX_VERSION,
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
            }
            for row in hits
        ],
        "limitations": [
            "检索命中是取证样本，不能用于计算主题发生率或证明因果。",
            "当前使用中文分词、英文词干、短语与别名检索；未启用向量检索，真实运营语料召回率待验收。",
        ],
    }
    audit(actor, shop, "evidence.retrieved", payload["retrieval_id"], after=payload)
    return payload
