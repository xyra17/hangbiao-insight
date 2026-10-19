from __future__ import annotations

import os
import re
from datetime import date
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from django.db import connection, transaction
from django.utils import timezone
from langgraph.graph import END, START, StateGraph
from psycopg.conninfo import make_conninfo
from rest_framework.exceptions import PermissionDenied

from insight.access import products_for, require_role
from insight.ingestion.contracts import digest
from insight.metrics.facts import authorize_object, source_manifest
from insight.models import (
    InvestigationDecision,
    InvestigationRun,
    InvestigationStep,
    MetricSnapshot,
    SemanticClaim,
    SemanticRun,
)
from insight.services import audit

from .checkpoints import FencedPostgresSaver
from .freeze import assert_same_inputs, validate_frozen_inputs
from .output import compose_output


class InvestigationState(TypedDict, total=False):
    query: str
    request_scope: dict
    content_query: str
    scope_query: str
    asin: str
    product_id: str | None
    tools: list[str]
    next_tool: str
    evidence: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    clarification: str
    result: dict[str, Any]
    stop_reason: str
    steps: int
    source_manifest: dict
    search_start: str
    search_end: str
    input_versions: dict
    write_requested: bool
    write_result: dict[str, Any]
    decisions: list[dict[str, Any]]
    counter_evidence: list[dict[str, Any]]
    planner: str
    candidates: list[dict[str, Any]]
    planner_provider: str
    tool_args: dict
    tool_decision: dict
    text_search_rounds: int
    executed_text_requests: list[list[str]]


def assert_execution_owner(run, locked):
    if locked.lease_token != run.lease_token:
        raise RuntimeError("investigation_lease_superseded")
    if hasattr(run, "_lock_pid"):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                "AND pid=%s AND classid=%s AND objid=%s AND objsubid=1 AND granted)",
                [run._lock_pid, run._lock_key >> 32, run._lock_key & 0xFFFFFFFF],
            )
            if not cursor.fetchone()[0]:
                raise RuntimeError("investigation_lock_lost")


def selected_product_id(run, state):
    return state.get("product_id") or (str(run.product_id) if run.product_id else None)


def evidence_driven_counter_search(query: str, results: list[dict[str, Any]]) -> dict:
    """Separate evidence intent from topic anchors and the immutable authorized scope."""
    from insight.semantic.retrieval import ALIASES, contains_term, context_anchor_groups
    from insight.semantic.safety_context import record_contexts

    groups = context_anchor_groups(query)
    properties = {ALIASES[index] for index in (0, 1, 2, 5)}
    if not any(group in properties for group in groups) and (contains_term(query, "safety") or "安全" in query):
        groups = [ALIASES[0]]
    contexts = {context["context"]
                for result in results for item in result.get("items", [])
                if item.get("match_kind", "relevant_hit") == "relevant_hit"
                for group in groups if group in properties
                for context in record_contexts(title=item.get("title", ""), text=item.get("text", ""), terms=group)}
    if "asserted" in contexts:
        context, rationale = "negated", "在原查询约束内查找对该属性的否定原文"
    elif contexts & {"negated", "hypothetical"}:
        context, rationale = "asserted", "在原查询约束内核查该属性是否实际发生"
    else:
        context, rationale = "any", "尚无可分类属性证据，保持原查询，不将未命中作为反证"
    return {"schema_version": "evidence-search-intent-v2", "query": query,
            "evidence_context": context, "optional_terms": [], "instruction": rationale,
            "observed_contexts": sorted(contexts),
            "scope_policy": "inherit_authorized_frozen_scope"}


def evidence_driven_counter_query(query: str, results: list[dict[str, Any]], fallback: str = "") -> str:
    # Compatibility helper returns only executable content anchors, never instructions.
    return evidence_driven_counter_search(query, results)["query"]


def _persist(run: InvestigationRun, node: str, state: InvestigationState, output: dict[str, Any], tool=""):
    with transaction.atomic():
        locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
        assert_execution_owner(run, locked)
        require_role(run.requested_by, run.shop, "operator", "quality", "support", "data_admin")
        if locked.status == "cancelled":
            raise RuntimeError("investigation_cancelled")
        if locked.steps_used >= locked.max_steps or locked.budget_units <= 0:
            raise RuntimeError("investigation_budget_exhausted")
        if locked.input_versions:
            assert_same_inputs(locked.input_versions, state.get("input_versions", {}))
        elif state.get("input_versions") is not None:
            validate_frozen_inputs(state.get("input_versions", {}))
        sequence = locked.steps_used + 1
        state["steps"] = sequence
        InvestigationStep.objects.create(
            run=locked,
            sequence=sequence,
            node=node,
            tool_name=tool,
            status="completed",
            input={"query": state.get("query", ""), "asin": state.get("asin", "")},
            output=output,
            evidence_refs=[item.get("id") for item in state.get("evidence", []) if item.get("id")],
            logical_key=(f"text_evidence:{state['text_search_rounds'] - 1}" if tool == "text_evidence" and state.get("text_search_rounds", 0) > 1 else tool or node),
            state_snapshot=dict(state),
        )
        locked.steps_used = sequence
        locked.budget_units = max(locked.budget_units - 1, 0)
        locked.checkpoint_version += 1
        locked.state = dict(state)
        locked.input_versions = state.get("input_versions", locked.input_versions)
        if state.get("asin"):
            product_id = selected_product_id(run, state)
            authorize_object(run.requested_by, run.shop, state["asin"], product_id)
            locked.child_asin = state["asin"]
            if product_id:
                locked.product_id = product_id
        locked.save(update_fields=["steps_used", "budget_units", "checkpoint_version", "state", "child_asin", "product", "input_versions"])
    audit(run.requested_by, run.shop, "investigation.step_completed", run.pk,
              after={"sequence": sequence, "logical_key": tool or node}, request_id=str(run.pk))


def _plan(run: InvestigationRun, state: InvestigationState, available_tools: list[str], reason: str):
    """Run the configured structured planner and persist its decision receipt."""
    require_role(run.requested_by, run.shop, "operator", "quality", "support", "data_admin")
    from .budget import MeasuredCall, reserve
    from .planner import ModelPlanner, PlannerDecision, planner_for

    history = [
        {"tool": item.get("next_tool"), "stop_reason": item.get("stop_reason", ""),
         "hypothesis": item.get("hypothesis", ""), "counter_evidence_query": item.get("counter_evidence_query", ""),
         "tool_args": item.get("tool_args", {})}
        for item in state.get("decisions", [])
    ]
    decision_key = digest({
        "run": str(run.pk),
        "phase": reason,
        "evidence": state.get("evidence", []),
        "input_versions": state.get("input_versions", {}),
    })
    provider = state.get("planner_provider") or run.state.get("planner_provider") or None
    from .execution_config import configuration_snapshot
    from .model_limits import validate_cost_envelope
    config = configuration_snapshot()
    external_planner = (provider or config['environment_default_planner']) in {'deepseek', 'deepseek-v4-flash'}
    if external_planner or config['relevance_enabled']:
        try:
            validate_cost_envelope(planner_active=external_planner, relevance_active=config['relevance_enabled'], max_steps=run.max_steps)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    entry = reserve(
        run,
        kind="model",
        operation=reason,
        idempotency_key=decision_key,
        units=1,
            metadata={"provider": provider or "deterministic", "input_versions": state.get("input_versions", {}),
                      "execution_configuration": config,
                      **({"reserved_max_cost": os.getenv("INVESTIGATION_MAX_CALL_COST", "")} if external_planner else {})},
    )
    # A worker can die after the durable budget reservation/settlement but
    # before the LangGraph checkpoint is written.  Keep the structured
    # decision on that same append-only ledger row so replay does not call a
    # non-deterministic provider twice or spend a second model allowance.
    cached = entry.metadata.get("decision") if isinstance(entry.metadata, dict) else None
    cached_reason = entry.metadata.get("reason") if isinstance(entry.metadata, dict) else None
    if isinstance(cached, dict) and cached_reason == reason:
        try:
            decision = PlannerDecision(**cached)
        except (TypeError, ValueError):
            decision = None
    else:
        decision = None
    if decision is None:
        measured = MeasuredCall(entry)
        selected_planner = None
        try:
            selected_planner = planner_for(provider)
            planner_context = {}
            if isinstance(selected_planner, ModelPlanner):
                from .policy import permitted_read_tools
                available_tools = permitted_read_tools(run, available_tools)
                planner_context = {"request_context": {
                    "shop": str(run.shop_id), "asin": state.get("asin"),
                    "product_id": selected_product_id(run, state),
                    "start": state.get("search_start"), "end": state.get("search_end"),
                    "source_kinds": sorted({source.get("kind") for source in state.get("source_manifest", {}).values() if source.get("kind")}),
                }}
            if external_planner:
                gateway = getattr(getattr(selected_planner, "planner", None), "gateway", None)
                if gateway is not None:
                    entry.metadata = {**entry.metadata,
                                      "dispatch_timeout_seconds": getattr(gateway, "timeout_seconds", 0)}
                    entry.save(update_fields=["metadata"])
                from .budget import begin_external_attempt
                begin_external_attempt(entry)
            decision = selected_planner.decide(
                query=state.get("query", ""),
                evidence=state.get("evidence", []),
                history=history,
                available_tools=available_tools,
                **planner_context,
            )
        except Exception as exc:
            from app.semantic.structured_response import usage_cost
            gateway = getattr(getattr(selected_planner, "planner", None), "gateway", None)
            receipt = getattr(gateway, "last_model_receipt", {})
            accounting = usage_cost(getattr(gateway, "last_usage", {})) if receipt.get("response_received") else {"tokens": 0, "cost": "0", "cost_known": False}
            measured.finish(tokens=accounting["tokens"], cost=accounting["cost"], metadata={
                "error": type(exc).__name__, "failure_code": getattr(exc, "code", None),
                "model_receipt": receipt, "usage": getattr(gateway, "last_usage", {}),
                "charge_outcome": "recorded_by_usage" if accounting["cost_known"] else "unknown_until_receipt"})
            raise
        data = decision.as_dict()
        decision_hash = digest({"run": str(run.pk), "phase": reason, "decision": data})
        measured.finish(
            tokens=decision.tokens,
            cost=decision.cost,
            metadata={"reason": reason, "decision": data, "decision_hash": decision_hash,
                      "charge_outcome": "recorded_by_usage" if decision.cost_estimate_known else "unknown"},
        )
    data = decision.as_dict()
    decision_hash = digest({"run": str(run.pk), "phase": reason, "decision": data})
    with transaction.atomic():
        locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
        sequence = len(state.get("decisions", [])) + 1
        InvestigationDecision.objects.get_or_create(
            run=locked,
            decision_hash=decision_hash,
            defaults={
                "sequence": sequence,
                "planner": decision.planner,
                "input_evidence": state.get("evidence", []),
                "hypothesis": decision.hypothesis,
                "next_tool": decision.next_tool,
                "tool_args": decision.tool_args or {},
                "counter_evidence_query": decision.counter_evidence_query,
                "stop_reason": decision.stop_reason,
                "confidence": decision.confidence,
            },
        )
        receipt = {"phase": reason, "decision_hash": decision_hash, **data}
        prior = locked.decisions if isinstance(locked.decisions, list) else []
        already_recorded = any(
            item.get("decision_hash") == decision_hash
            for item in prior
            if isinstance(item, dict)
        )
        if not already_recorded:
            locked.decisions = [*prior, receipt]
            locked.save(update_fields=["decisions"])
    state_decisions = state.setdefault("decisions", [])
    if not any(
        isinstance(item, dict)
        and item.get("decision_hash") == decision_hash
        for item in state_decisions
    ):
        state_decisions.append({"phase": reason, "decision_hash": decision_hash, **data})
    state["tool_decision"] = {"decision_hash": decision_hash, "phase": reason, "next_tool": decision.next_tool}
    state["tool_args"] = decision.tool_args or {}
    state["planner"] = decision.planner
    if decision.clarification:
        state["clarification"] = decision.clarification
    if decision.counter_evidence_query:
        state["counter_evidence_query"] = decision.counter_evidence_query
    return decision


def build_graph(run: InvestigationRun, checkpointer=None):
    def locate(state: InvestigationState):
        require_role(run.requested_by, run.shop, "operator", "quality", "support", "data_admin")
        if run.input_versions:
            if not state.get("input_versions"):
                state["input_versions"] = run.input_versions
                state["source_manifest"] = run.input_versions.get("source_manifest", {})
            else:
                assert_same_inputs(run.input_versions, state["input_versions"])
        if "source_manifest" not in state:
            state["source_manifest"] = source_manifest(run.shop)
        from .scope import corrected_content_query, resolve_scope
        if "request_scope" not in state:
            frozen_scope = state.get("input_versions", {}).get("request_scope")
            end = date.fromisoformat(state["search_end"]) if state.get("search_end") else timezone.now().astimezone(ZoneInfo(run.shop.timezone)).date()
            state["request_scope"] = frozen_scope or resolve_scope(state.get("scope_query", state["query"]), today=end, timezone=run.shop.timezone)
            if not frozen_scope and state.get("scope_query"):
                if state["request_scope"]["status"] == "default":
                    state["request_scope"].update(status="needs_clarification", start=None, end=None,
                        reason="补充内容尚未明确原请求的日期限定，请提供完整起止日期；不会改用默认窗口。")
                state["request_scope"]["resolution_query"] = state["scope_query"]
                state["request_scope"]["content_query"] = corrected_content_query(state["query"])
        scope = state["request_scope"]
        state["content_query"] = scope["content_query"]
        if scope["status"] == "needs_clarification":
            state["clarification"] = scope["reason"]
        else:
            state["search_start"], state["search_end"] = scope["start"], scope["end"]
        query = state["query"]
        asin = state.get("asin") or next(iter(re.findall(r"\b[A-Z0-9]{10}\b", query.upper())), "")
        if not asin:
            # Fuzzy routing is restricted to the caller's current shop grant;
            # an ungranted shop is never used to reveal a candidate count/name.
            query_lower = query.lower()
            scoped = products_for(run.requested_by).filter(shop=run.shop).prefetch_related("mappings")
            matched = []
            for product in scoped:
                aliases = {product.child_asin.lower(), product.seller_sku.lower()}
                aliases.update(mapping.name.lower() for mapping in product.mappings.all())
                aliases.update(mapping.parent_asin.lower() for mapping in product.mappings.all() if mapping.parent_asin)
                if any(alias and alias in query_lower for alias in aliases):
                    matched.append(product)
            if not matched:
                matched = list(scoped)
            distinct = {(item.child_asin, str(item.pk), item.seller_sku) for item in matched}
            candidates = [
                {"child_asin": child, "product_id": product_id, "seller_sku": sku}
                for child, product_id, sku in sorted(distinct)
            ]
            state["candidates"] = candidates[:20]
            asin_values = sorted({item["child_asin"] for item in candidates})
            if len(asin_values) == 1:
                asin = asin_values[0]
                matching_products = [item for item in candidates if item["child_asin"] == asin]
                if len(matching_products) == 1:
                    state["product_id"] = matching_products[0]["product_id"]
            elif not asin_values:
                state["clarification"] = "没有找到当前店铺中可访问的商品，请提供子 ASIN 或 SKU。"
            else:
                state["clarification"] = "找到多个可访问商品，请补充子 ASIN、SKU 或名称。"
        elif asin:
            authorized_products = list(
                products_for(run.requested_by).filter(shop=run.shop, child_asin=asin)
                .values("id", "seller_sku").order_by("seller_sku", "id")
            )
            if run.product_id:
                state["product_id"] = str(run.product_id)
            else:
                try:
                    authorize_object(run.requested_by, run.shop, asin, None)
                except PermissionDenied:
                    if len(authorized_products) == 1:
                        state["product_id"] = str(authorized_products[0]["id"])
                    elif authorized_products:
                        state["candidates"] = [
                            {"child_asin": asin, "product_id": str(item["id"]), "seller_sku": item["seller_sku"]}
                            for item in authorized_products[:20]
                        ]
                        state["clarification"] = "该 ASIN 下存在多个可访问 SKU，请选择本次调查对象。"
                    else:
                        state["clarification"] = "该对象不在当前调查授权范围内，请选择当前店铺可访问的商品。"
        if asin and not state.get("clarification") and not selected_product_id(run, state):
            try:
                authorize_object(run.requested_by, run.shop, asin, None)
            except PermissionDenied:
                authorized_products = list(
                    products_for(run.requested_by).filter(shop=run.shop, child_asin=asin)
                    .values("id", "seller_sku").order_by("seller_sku", "id")
                )
                if len(authorized_products) == 1:
                    state["product_id"] = str(authorized_products[0]["id"])
                else:
                    state["candidates"] = [
                        {"child_asin": asin, "product_id": str(item["id"]), "seller_sku": item["seller_sku"]}
                        for item in authorized_products[:20]
                    ]
                    state["clarification"] = "请在当前授权范围内选择一个 SKU。" if authorized_products else "该对象不在当前调查授权范围内，请确认店铺和商品权限。"
        if state.get("clarification"):
            asin = ""
        if asin:
            product_id = selected_product_id(run, state)
            authorize_object(run.requested_by, run.shop, asin, product_id)
            state["asin"] = asin
            frozen_metric = MetricSnapshot.objects.filter(
                shop=run.shop, child_asin=asin, product_id=product_id,
                source_manifest=state["source_manifest"],
                **({"pk": state["input_versions"]["metric_snapshot"]} if run.read_only_refresh else {}),
            ).exclude(payload__contains={"mode": "historical"}).order_by("-created_at").first()
            frozen_semantic = SemanticRun.objects.filter(
                shop=run.shop, child_asin=asin, product_id=product_id,
                source_manifest=state["source_manifest"], status__in=["completed", "partial"],
                **({"pk": state["input_versions"]["semantic_run"]} if run.read_only_refresh else {}),
            ).order_by("-created_at").first()
            state["input_versions"] = {
                "metric_snapshot": str(frozen_metric.pk) if frozen_metric else None,
                "semantic_run": str(frozen_semantic.pk) if frozen_semantic else None,
                "source_manifest": state["source_manifest"],
                "mapping_versions": frozen_metric.payload.get("mapping_versions", {}) if frozen_metric else {},
                "rule_version": frozen_metric.rule_version if frozen_metric else None,
                "workflow_version": state.get("input_versions", {}).get("workflow_version", "investigation-evidence-v3"),
                "request_scope": state["request_scope"],
            }
            validate_frozen_inputs(state["input_versions"])
        else:
            state["product_id"] = str(run.product_id) if scope["status"] == "needs_clarification" and run.child_asin and run.product_id else None
        _persist(run, "locate", state, {"asin": asin, "candidates": state.get("candidates", []), "clarification": state.get("clarification", "")})
        return state

    def choose(state: InvestigationState):
        text = state["query"].lower()
        decision = _plan(run, state, ["metric_snapshot", "order_records", "review_claims", "text_evidence"], "route")
        tools = [decision.next_tool] if decision.next_tool else []
        # Internal writes are explicit and bounded.  A normal investigation stays
        # read-only; only a request for a follow-up/check task enables the write node.
        explicit_write = any(
            word in text for word in ("创建任务", "建立任务", "核查任务", "跟进任务", "安排核查", "create task", "follow-up")
        )
        refusal = any(
            word in text for word in (
                "不要创建", "不要建立", "不要安排", "无需创建", "无需建立", "不要生成",
                "do not create", "without task",
            )
        )
        # A UI/API opt-in is the strongest signal. Natural-language intent is
        # accepted only when the query is affirmative; negated text can never
        # turn on a write capability by itself.
        state["write_requested"] = bool(state.get("write_requested")) or (explicit_write and not refusal)
        if any(cue in state.get("query", "") for cue in ("修改价格", "修改库存", "修改Listing", "批准变更")):
            state.setdefault("evidence", []).append({"type": "authorization_boundary", "external_change_requested": True,
                                                     "external_write_authorized": False})
        state["tools"] = list(dict.fromkeys(tools))
        state["next_tool"] = decision.next_tool
        _persist(run, "choose", state, {"tools": state["tools"], "write_requested": state["write_requested"], "planner": decision.as_dict()})
        return state

    def metric(state: InvestigationState):
        from .policy import authorize_tool, record_tool_result

        call = authorize_tool(run, "metric_snapshot", state, idempotency_key=f"metric:{state.get('steps', 0)}")
        product_id = selected_product_id(run, state)
        authorize_object(run.requested_by, run.shop, state["asin"], product_id)
        snapshot = (
            MetricSnapshot.objects.filter(pk=state.get("input_versions", {}).get("metric_snapshot"), shop=run.shop, child_asin=state["asin"], product_id=product_id)
            .exclude(payload__contains={"mode": "historical"})
            .order_by("-created_at")
            .first()
        )
        if snapshot:
            from insight.metrics.api import can_read_raw_source

            from .orders import redact_order_ids
            metrics = snapshot.payload if can_read_raw_source(run.requested_by, run.shop) else redact_order_ids(snapshot.payload)
            signals = []
            for section in ("daily", "absolute", "cohort", "occurrence"):
                signals.extend(snapshot.payload.get(section, {}).get("signals", []))
            from .citations import metric_catalog
            state.setdefault("evidence", []).append(
                {"id": str(snapshot.pk), "type": "metric_snapshot", "signals": signals,
                 "metrics": metrics, "request_scope": state.get("request_scope"),
                 "metric_evidence": metric_catalog(snapshot_id=snapshot.pk, snapshot_fingerprint=snapshot.fingerprint,
                                                   source_manifest=snapshot.source_manifest, shop_id=snapshot.shop_id,
                                                   child_asin=snapshot.child_asin, product_id=snapshot.product_id,
                                                   metrics=snapshot.payload)}
            )
            state.setdefault("hypotheses", []).extend(
                {
                    "kind": "signal",
                    "metric": signal.get("metric"),
                    "title": signal.get("title"),
                    "confidence": "rule",
                }
                for signal in signals
            )
        else:
            state.setdefault("evidence", []).append(
                {"id": "metric:none", "type": "metric_snapshot", "status": "missing", "request_scope": state.get("request_scope")}
            )
        record_tool_result(call, refs=[str(snapshot.pk)] if snapshot else ["metric:none"])
        _persist(run, "execute", state, {"found": bool(snapshot)}, "metric_snapshot")
        decision = _plan(run, state, ["order_records", "review_claims", "text_evidence"], "after_metric")
        state["next_tool"] = decision.next_tool
        return state

    def orders(state: InvestigationState):
        from .orders import read_order_records
        from .policy import authorize_tool, record_tool_result
        call = authorize_tool(run, "order_records", state, idempotency_key=f"orders:{state.get('steps', 0)}")
        result = read_order_records(actor=run.requested_by, shop=run.shop, asin=state["asin"],
                                    product_id=selected_product_id(run, state), manifest=state["source_manifest"],
                                    start=state["search_start"], end=state["search_end"])
        state.setdefault("evidence", []).append(result)
        record_tool_result(call, refs=[item["fact_id"] for section in result["sections"].values() for item in section["items"]])
        _persist(run, "execute", state, result, "order_records")
        _plan(run, state, [], "after_orders")
        return state

    def reviews(state: InvestigationState):
        from .policy import authorize_tool, record_tool_result

        call = authorize_tool(run, "review_claims", state, idempotency_key=f"reviews:{state.get('steps', 0)}")
        product_id = selected_product_id(run, state)
        authorize_object(run.requested_by, run.shop, state["asin"], product_id)
        semantic = (
            SemanticRun.objects.filter(
                pk=state.get("input_versions", {}).get("semantic_run"),
                shop=run.shop,
                child_asin=state["asin"],
                product_id=product_id,
                status__in=["completed", "partial"],
            )
            .order_by("-created_at")
            .first()
        )
        claims = (
            list(SemanticClaim.objects.filter(
                run=semantic, prediction_status="accepted",
                review__business_date__range=(state["search_start"], state["search_end"]),
            ).order_by("review__business_date", "pk")[:50])
            if semantic
            else []
        )
        if claims:
            state.setdefault("evidence", []).append(
                {"id": str(semantic.pk), "type": "semantic_run", "claim_count": len(claims), "request_scope": state.get("request_scope")}
            )
            state.setdefault("hypotheses", []).extend(
                {
                    "kind": "review",
                    "topic": claim.secondary_topic,
                    "polarity": claim.polarity,
                    "evidence_span": claim.evidence_span,
                    "claim_id": str(claim.pk),
                    "business_date": claim.review.business_date.isoformat(),
                }
                for claim in claims
            )
        else:
            state.setdefault("evidence", []).append(
                {"id": "review:none", "type": "semantic_run", "status": "missing"}
            )
        record_tool_result(call, refs=[str(semantic.pk)] if semantic else ["review:none"])
        _persist(run, "execute", state, {"claim_count": len(claims)}, "review_claims")
        decision = _plan(run, state, ["text_evidence"], "after_reviews")
        state["next_tool"] = decision.next_tool
        return state

    def search_text(state: InvestigationState):
        from insight.semantic.retrieval import retrieve

        from .policy import authorize_tool, record_tool_result

        call = authorize_tool(run, "text_evidence", state, idempotency_key=f"text:{state.get('steps', 0)}")

        results = []

        def search(query: str, phase: str, evidence_context="any", intent=None, parents=None):
            phase_results = []
            for kind in ("reviews", "support"):
                result = retrieve(
                    run.requested_by,
                    run.shop,
                    state["asin"],
                    query=query,
                    start=date.fromisoformat(state["search_start"]),
                    end=date.fromisoformat(state["search_end"]),
                    product_id=selected_product_id(run, state),
                    kind=kind,
                    limit=10,
                    manifest=state["source_manifest"],
                    evidence_context=evidence_context,
                    budget_run=run,
                )
                result["origin_decision_hash"] = state.get("tool_decision", {}).get("decision_hash")
                result["derived_from_retrieval_ids"] = parents or []
                if query_binding and query == search_query:
                    result["query_scope_binding"] = query_binding
                result["request_scope"] = state.get("request_scope")
                result["request_query"] = state["query"]
                results.append(result)
                phase_results.append(result)
                state.setdefault("evidence", []).append(
                    {
                        "id": result["retrieval_id"],
                        "type": "text_retrieval",
                        "phase": phase,
                        "retrieval_id": result["retrieval_id"],
                        "origin_decision_hash": result["origin_decision_hash"],
                        "derived_from_retrieval_ids": result["derived_from_retrieval_ids"],
                        "query": query,
                        "search_intent": intent,
                        **({"query_scope_binding": query_binding} if query_binding and query == search_query else {}),
                        "evidence_context": result["evidence_context"],
                        "retrieval_complete": result["retrieval_complete"],
                        "relevance_judgment": result["relevance_judgment"],
                        "candidate_stage": result.get("candidate_stage", {}),
                        "scope": result["scope"],
                        "request_scope": state.get("request_scope"),
                        "request_query": state["query"],
                        "source_manifest": result["source_manifest"],
                        "items": result["items"],
                        "limitations": result["limitations"],
                        "match_status": result["match_status"],
                        "retrieval_version": result["retrieval_version"],
                        "algorithm_manifest": result["algorithm_manifest"],
                    }
                )
            return phase_results

        args = state.get("tool_args", {})
        if not isinstance(args, dict) or set(args) - {"query", "evidence_context"}:
            raise RuntimeError("unsupported_text_evidence_arguments")
        search_query = args.get("query", state.get("content_query", state["query"]))
        context = args.get("evidence_context", "any")
        if not isinstance(search_query, str) or not 2 <= len(search_query.strip()) <= 2000:
            raise RuntimeError("invalid_text_evidence_query")
        if context not in {"any", "asserted", "negated", "hypothetical"}:
            raise RuntimeError("invalid_text_evidence_context")
        # The planner chooses content; redundant identical scope is bound by
        # the server, conflicting/ambiguous scope still fails before retrieval.
        from .query_binding import bind_text_query
        try:
            search_query, query_binding = bind_text_query(search_query, {
                "shop": str(run.shop_id), "asin": state["asin"], "product_id": selected_product_id(run, state),
                "start": state["search_start"], "end": state["search_end"],
            })
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        previous_requests = state.get("executed_text_requests", [])
        if [search_query, context] in previous_requests:
            raise RuntimeError("duplicate_text_evidence_request")
        state["executed_text_requests"] = previous_requests + [[search_query, context]]
        model_driven = (state.get("planner_provider") or os.getenv("INVESTIGATION_PLANNER_PROVIDER", "deterministic")) in {"deepseek", "deepseek-v4-flash"}
        rounds = int(state.get("text_search_rounds", 0))
        parents = [item["id"] for item in state.get("evidence", []) if item.get("type") == "text_retrieval"]
        initial_results = search(search_query, "model_follow_up" if model_driven and rounds else "initial", context,
                                 parents=parents if rounds else None)

        intent = evidence_driven_counter_search(search_query, initial_results)
        follow_up_query = intent["query"]
        state["counter_evidence_query"] = follow_up_query
        state["counter_evidence_search_intent"] = intent
        from insight.semantic.retrieval import query_groups
        if model_driven or intent["evidence_context"] == "any" and query_groups(follow_up_query) == query_groups(search_query):
            counter_results = []
            state["counter_search_status"] = "planner_controls_follow_up" if model_driven else "no_distinct_search_intent"
        else:
            counter_results = search(follow_up_query, "counter_evidence", intent["evidence_context"], intent, [row["retrieval_id"] for row in initial_results])
            state["counter_search_status"] = "executed"
        for result in counter_results:
            state.setdefault("counter_evidence", []).append(
                {
                    "query": follow_up_query,
                    "retrieval_id": result["retrieval_id"],
                    "kind": result["scope"]["kind"],
                    "items": result["items"],
                    "scope": result["scope"],
                }
            )
        record_tool_result(call, refs=[item["id"] for item in state.get("evidence", []) if item.get("type") == "text_retrieval"])
        state["text_search_rounds"] = rounds + 1
        _persist(run, "execute", state, {"retrievals": results}, "text_evidence")
        follow_up_tools = ["text_evidence"] if model_driven and rounds < 1 and state["steps"] + 2 < run.max_steps else []
        decision = _plan(run, state, follow_up_tools, "after_text")
        state["next_tool"] = decision.next_tool
        return state

    def compose(state: InvestigationState):
        if state.get("write_requested") and "write_result" not in state:
            state["write_result"] = {
                "created": False,
                "status": "skipped",
                "reason": "调查剩余步骤不足以安全完成内部任务写入和结果保存。",
            }

        from .citations import recheck_metric_evidence
        recheck_metric_evidence(run, state.get("evidence", []))
        from .orders import read_order_records
        for entry in state.get("evidence", []):
            if entry.get("type") == "order_records":
                fresh = read_order_records(actor=run.requested_by, shop=run.shop, asin=state["asin"],
                                           product_id=selected_product_id(run, state), manifest=state["source_manifest"],
                                           start=state["search_start"], end=state["search_end"])
                if fresh != entry:
                    raise RuntimeError("frozen_order_records_changed")
        composed = compose_output(state.get("evidence", []), state.get("decisions", []))
        state["result"] = {
            **composed,
            "asin": state.get("asin", ""),
            "request_scope": state.get("request_scope"),
            "product_id": state.get("product_id") or (str(run.product_id) if run.product_id else None),
            "hypotheses": state.get("hypotheses", []),
            "evidence": state.get("evidence", []),
            "decisions": state.get("decisions", []),
            "counter_evidence": state.get("counter_evidence", []),
            "candidates": state.get("candidates", []),
            "write": state.get("write_result"),
            "limitations": [
                "未配置真实供应商时使用确定性 planner；真实模型质量和成本仍待业务验收。",
                f"原文检索按冻结请求窗口 {state.get('search_start')} 至 {state.get('search_end')}；检索样本不能证明因果或代表总体频率。",
            ] + composed["limitations"],
        }
        state["stop_reason"] = (
            state.get("decisions", [])[-1].get("stop_reason")
            if state.get("decisions") and state.get("decisions", [])[-1].get("stop_reason")
            else ("已完成允许的证据核查" if state.get("evidence") else "没有可用证据")
        )
        _persist(run, "compose", state, {"stop_reason": state["stop_reason"]})
        return state

    def write_task(state: InvestigationState):
        """Create one unassigned internal follow-up through the domain command.

        The graph never receives a direct model/database write tool.  The command
        validates scope, frozen evidence, run ownership and idempotency.  A replay
        after a worker crash therefore returns the existing command receipt instead
        of creating another task.
        """
        from .commands import create_draft_task
        from .policy import authorize_tool, record_tool_result

        call = authorize_tool(run, "create_draft_task", state, idempotency_key=f"write:{run.pk}")

        evidence_refs = [
            str(item["id"])
            for item in state.get("evidence", [])
            if item.get("id") and item.get("status") != "missing"
        ]
        if not evidence_refs:
            state["write_result"] = {
                "created": False,
                "status": "skipped",
                "reason": "没有可引用的已保存证据，未创建内部任务。",
            }
            record_tool_result(call, refs=[], status="skipped")
            _persist(run, "write_task", state, state["write_result"], "create_draft_task")
            return state
        asin = state.get("asin") or run.child_asin
        result = create_draft_task(
            run,
            {
                "logical_action_id": f"follow-up:{run.pk}",
                "title": f"核查 {asin} 调查发现",
                "question": "核对调查发现的异常方向，并记录人工核验结论。",
                "completion_criteria": "保留核验结果、处理依据和未解决限制。",
                "evidence_refs": evidence_refs,
            },
        )
        state["write_result"] = {"status": "created" if result.get("created") else "reused", **result}
        record_tool_result(call, refs=evidence_refs)
        _persist(run, "write_task", state, state["write_result"], "create_draft_task")
        return state

    def replayable(key, function):
        def invoke(state):
            require_role(run.requested_by, run.shop, "operator", "quality", "support", "data_admin")
            if key == "text_evidence" and int(state.get("text_search_rounds", 0)) >= 2:
                raise RuntimeError("text_evidence_round_limit")
            current = InvestigationRun.objects.get(pk=run.pk)
            assert_execution_owner(run, current)
            if current.status == "cancelled":
                raise RuntimeError("investigation_cancelled")
            if current.child_asin:
                authorize_object(run.requested_by, run.shop, current.child_asin, current.product_id)
            logical_keys = [key]
            if key == "text_evidence" and state.get("text_search_rounds", 0):
                logical_keys = [f"text_evidence:{state['text_search_rounds']}"]
            if key == "write_task":
                # The persisted command receipt uses the concrete tool name;
                # the graph node is named after the user-facing capability.
                logical_keys.append("create_draft_task")
            saved = InvestigationStep.objects.filter(run=run, logical_key__in=logical_keys).order_by("sequence").first()
            if saved:
                restored = dict(saved.state_snapshot)
                from .scope import validate_scope_state

                validate_scope_state(restored)
                if current.input_versions:
                    # The node receipt is committed before its LangGraph
                    # checkpoint. After a worker dies in that gap, the graph
                    # may replay from an older checkpoint with empty inputs;
                    # validate the durable node snapshot that will actually be
                    # replayed before trusting it.
                    assert_same_inputs(current.input_versions, restored.get("input_versions", {}))
                # Tool facts commit before their following model decision. A crash
                # in that gap must reuse the durable decision ledger, not loop on
                # the previous next_tool or re-read/re-dispatch the tool.
                tools_and_phase = {"metric_snapshot": (["order_records", "review_claims", "text_evidence"], "after_metric"),
                                   "review_claims": (["text_evidence"], "after_reviews"), "order_records": ([], "after_orders")}
                if key == "text_evidence":
                    external = (restored.get("planner_provider") or os.getenv("INVESTIGATION_PLANNER_PROVIDER", "deterministic")) in {"deepseek", "deepseek-v4-flash"}
                    rounds = int(restored.get("text_search_rounds", 1))
                    tools_and_phase[key] = (["text_evidence"] if external and rounds < 2 and restored["steps"] + 2 < run.max_steps else [], "after_text")
                if key in tools_and_phase:
                    tools, phase = tools_and_phase[key]
                    restored["next_tool"] = _plan(run, restored, tools, phase).next_tool
                return restored
            from .scope import validate_scope_state

            # A successor inherits the parent's window before its own locate
            # freezes the new input version. Resumed runs must still validate.
            if key != "locate" or current.input_versions or current.steps.exists():
                validate_scope_state(state)
            if current.input_versions:
                assert_same_inputs(current.input_versions, state.get("input_versions", {}))
            # Each domain command and checkpoint owns its own transaction.  Do
            # not wrap the whole node in one outer transaction: an internal
            # task may commit before a worker crashes while recording the
            # checkpoint, and the command receipt must make the replay reuse
            # that task instead of rolling it back or duplicating it.
            with transaction.atomic():
                locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
                assert_execution_owner(run, locked)
            return function(state)
        return invoke

    graph = StateGraph(InvestigationState)
    for key, function in (("locate", locate), ("choose", choose), ("metric_snapshot", metric),
                          ("order_records", orders), ("review_claims", reviews), ("text_evidence", search_text),
                          ("write_task", write_task), ("compose", compose)):
        graph.add_node(key, replayable(key, function))
    graph.add_edge(START, "locate")
    graph.add_conditional_edges(
        "locate",
        lambda state: "clarify" if state.get("clarification") else "choose",
        {"clarify": END, "choose": "choose"},
    )
    graph.add_conditional_edges(
        "choose",
        lambda state: "clarify" if state.get("clarification") else state.get("next_tool") or "compose",
        {"metric_snapshot": "metric_snapshot", "order_records": "order_records", "review_claims": "review_claims",
         "text_evidence": "text_evidence", "compose": "compose", "clarify": END},
    )
    graph.add_conditional_edges(
        "metric_snapshot",
        lambda state: "clarify" if state.get("clarification") else state.get("next_tool") if state.get("next_tool") in {"order_records", "review_claims", "text_evidence"} else (
            "write_task"
            if state.get("write_requested") and int(state.get("steps", run.steps_used)) + 2 <= run.max_steps
            else "compose"
        ),
        {
            "order_records": "order_records",
            "review_claims": "review_claims",
            "text_evidence": "text_evidence",
            "write_task": "write_task",
            "compose": "compose",
            "clarify": END,
        },
    )
    graph.add_conditional_edges(
        "review_claims",
        lambda state: "clarify" if state.get("clarification") else state.get("next_tool") if state.get("next_tool") == "text_evidence" else (
            "write_task" if state.get("write_requested") and int(state.get("steps", run.steps_used)) + 2 <= run.max_steps else "compose"
        ),
        {"text_evidence": "text_evidence", "write_task": "write_task", "compose": "compose", "clarify": END},
    )
    graph.add_conditional_edges(
        "text_evidence",
        lambda state: "clarify" if state.get("clarification") else "text_evidence" if state.get("next_tool") == "text_evidence" else "write_task"
        if state.get("write_requested") and int(state.get("steps", run.steps_used)) + 2 <= run.max_steps
        else "compose",
        {"text_evidence": "text_evidence", "write_task": "write_task", "compose": "compose", "clarify": END},
    )
    graph.add_conditional_edges("order_records", lambda state: "clarify" if state.get("clarification") else "compose", {"clarify": END, "compose": "compose"})
    graph.add_edge("write_task", "compose")
    graph.add_edge("compose", END)
    return graph.compile(checkpointer=checkpointer)


def execute_investigation(run: InvestigationRun):
    state: InvestigationState = {
        "query": run.query,
        "asin": run.child_asin,
        "product_id": str(run.product_id) if run.product_id else None,
        "evidence": [],
        "hypotheses": [],
        "steps": run.steps_used,
        "write_requested": bool(run.state.get("write_requested")),
        "decisions": list(run.state.get("decisions", [])),
        "counter_evidence": list(run.state.get("counter_evidence", [])),
        "candidates": list(run.state.get("candidates", [])),
        "planner_provider": str(run.state.get("planner_provider") or ""),
    }
    if run.state.get("scope_query"):
        state["scope_query"] = run.state["scope_query"]
    if run.input_versions:
        # A worker may die after the node transaction commits but before its
        # LangGraph checkpoint. Replay must use the durable frozen manifest
        # and input IDs already written by that node, even for a normal run.
        state["source_manifest"] = run.input_versions.get("source_manifest", {})
        state["input_versions"] = run.input_versions
    if run.read_only_refresh and "source_manifest" in run.state:
        state["source_manifest"] = run.state["source_manifest"]
        if not run.input_versions and "input_versions" in run.state:
            # Refresh successors are created with explicit parent snapshot IDs
            # before their own first node freezes the complete manifest.
            state["input_versions"] = run.state["input_versions"]
        for key in ("request_scope", "search_start", "search_end"):
            if key in run.state:
                state[key] = run.state[key]
    config = {"configurable": {"thread_id": str(run.pk)}, "recursion_limit": max(8, run.max_steps * 2 + 4)}
    db = connection.settings_dict
    conninfo = make_conninfo(
        dbname=db["NAME"], user=db["USER"], password=db["PASSWORD"], host=db["HOST"], port=db["PORT"]
    )
    with FencedPostgresSaver.from_conn_string(conninfo) as saver:
        saver.conn.execute("SELECT pg_advisory_lock(78201344)")
        try:
            saver.setup()
        finally:
            saver.conn.execute("SELECT pg_advisory_unlock(78201344)")
        key = run.pk.int % (2**63 - 1)
        lock = saver.conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired, pg_backend_pid() AS pid", (key,)).fetchone()
        if not lock["acquired"]:
            raise RuntimeError("investigation_already_running")
        run._lock_pid, run._lock_key = lock["pid"], key
        with transaction.atomic():
            locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
            if locked.status in {"completed", "cancelled", "needs_clarification"}:
                return locked
            if locked.steps.filter(logical_key="").exists():
                locked.status, locked.stop_reason = "partial", "旧调查没有可验证的输入冻结记录，请发起新调查。"
                locked.save(update_fields=["status", "stop_reason"])
                return locked
            locked.lease_token += 1
            locked.status = "running"
            locked.save(update_fields=["lease_token", "status"])
            run.lease_token = locked.lease_token
        saver.run_id, saver.lease_token = run.pk, run.lease_token
        try:
            graph = build_graph(run, checkpointer=saver)
            existing = graph.get_state(config)
            result = graph.invoke(None if existing.values else state, config=config)
        except PermissionDenied:
            with transaction.atomic():
                locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
                assert_execution_owner(run, locked)
                if locked.status != "cancelled":
                    locked.status, locked.stop_reason = "partial", "investigation_permission_denied"
                    locked.save(update_fields=["status", "stop_reason"])
            raise
        except RuntimeError as exc:
            with transaction.atomic():
                locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
                assert_execution_owner(run, locked)
                if locked.status != "cancelled":
                    locked.status, locked.stop_reason = "partial", str(exc)
                    locked.save(update_fields=["status", "stop_reason"])
                return locked
        with transaction.atomic():
            locked = InvestigationRun.objects.select_for_update().get(pk=run.pk)
            assert_execution_owner(run, locked)
            if locked.status == "cancelled":
                return locked
            require_role(run.requested_by, run.shop, "operator", "quality", "support", "data_admin")
            if result.get("asin"):
                authorize_object(run.requested_by, run.shop, result["asin"], result.get("product_id") or run.product_id)
            if result.get("clarification"):
                locked.status, locked.stop_reason, locked.result = (
                    "needs_clarification", result["clarification"], {
                        **compose_output([], []),
                        "clarification": result["clarification"],
                        "request_scope": result.get("request_scope"),
                        "candidates": result.get("candidates", []),
                        "input_versions": result.get("input_versions", {}),
                    },
                )
            else:
                locked.status, locked.stop_reason, locked.result = (
                    "completed", result.get("stop_reason", ""), result.get("result", {}),
                )
            locked.state = result
            locked.save(update_fields=["status", "stop_reason", "result", "state"])
            audit(run.requested_by, run.shop, "investigation.finished", run.pk,
                  after={"status": locked.status, "input_versions": locked.input_versions}, request_id=str(run.pk))
            return locked
