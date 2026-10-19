"""Planner adapter boundary for bounded investigation decisions.

The deterministic planner is intentionally explicit and testable.  A real
provider can implement the same structured interface once its region, budget
and data-transfer approval is configured; no provider response is allowed to
execute a tool without the domain policy check in ``policy.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any


def planner_fingerprint():
    paths = [Path(__file__), Path(__file__).with_name("graph.py"), Path(__file__).with_name("output.py"), Path(__file__).with_name("business_schema.py"), Path(__file__).with_name("fact_followup.py"), Path(__file__).with_name("citations.py"), Path(__file__).with_name("orders.py"), Path(__file__).with_name("scope.py"), Path(__file__).with_name("freeze.py"),
             Path(__file__).parents[2] / "app" / "agent" / "planner.py",
             Path(__file__).parents[2] / "app" / "agent" / "output_contract.py",
             Path(__file__).parents[2] / "app" / "agent" / "investigation.py",
             Path(__file__).parents[2] / "app" / "semantic" / "factory.py", Path(__file__).with_name("policy.py"),
             Path(__file__).parent.parent / "semantic" / "safety_context.py",
             Path(__file__).parent.parent / "semantic" / "relevance.py",
             Path(__file__).parent.parent / "semantic" / "retrieval.py",
             Path(__file__).parent.parent / "semantic" / "vector_candidates.py"]
    return hashlib.sha256(b"".join(path.read_bytes() for path in paths)).hexdigest()


@dataclass(frozen=True)
class PlannerDecision:
    next_tool: str = ""
    tool_args: dict[str, Any] | None = None
    hypothesis: str = ""
    counter_evidence_query: str = ""
    stop_reason: str = ""
    clarification: str = ""
    confidence: float | None = None
    planner: str = "deterministic-investigation-v10"
    planner_sha256: str = field(default_factory=planner_fingerprint)
    final_answer: str = ""
    conclusion: dict[str, Any] | None = None
    business_contract: dict[str, Any] | None = None
    business_basis: dict[str, Any] | None = None
    model_receipt: dict[str, Any] | None = None
    execution_origin: str = "local_rule"
    tokens: int = 0
    cost: str = "0"
    cost_estimate_known: bool = True

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["tool_args"] = value["tool_args"] or {}
        return value


class PlannerUnavailable(RuntimeError):
    pass


def _has_positive_additional_evidence_intent(query: str) -> bool:
    """Keep causes and record searches out of a pure daily-metric completion."""
    clauses = re.split(r"[,，;；。!?！？\n]|并且|同时|以及|但是|但|\band\b|\bbut\b|\bhowever\b", (query or "").lower())
    negated = re.compile(r"^(?:(?:也|且|并且|同时|另外)\s*)?(?:不|无需|不需要|不要|不必|不会|不能|不得|do not\b|don't\b|without\b|not\b)")
    negated_cause = re.compile(
        r"(?:不(?:再|要|需要|用|必|作|做)?|无需|不用|无须|do not|don't|without|not)\s*"
        r"(?:再|去|进一步|进行|推断|判断|分析|研究|归因|证明|解释|infer|judge|analy[sz]e|attribute|prove|explain)?\s*"
        r"(?:经营)?\s*(?:原因|成因|因果|cause(?:s)?|reason(?:s)?|causal(?:ity)?)",
        re.IGNORECASE,
    )
    cause_terms = (
        "原因", "成因", "为什么", "为何", "导致", "造成", "归因", "因果", "因素", "解释",
        "cause", "reason", "why", "driver", "explain", "attribut",
    )
    investigation_terms = (
        "调查", "分析", "核查", "排查", "对比", "比较", "investigate", "investigation",
        "compare", "comparison", "contrast",
    )
    record_terms = (
        "评论", "原文", "证据", "工单", "客服", "售后", "包装", "过热", "受损", "损坏", "破损",
        "运输", "物流", "安全", "照片", "图片", "个案", "review", "original", "evidence", "ticket",
        "support", "complaint", "packaging", "overheat", "damage", "shipping", "transit", "carrier",
        "safety", "photo", "record", "customer",
    )
    comparison_terms = ("对比", "比较", "compare", "comparison", "contrast")
    exclusive_metric_comparison = (
        re.search(r"\bonly\b|只|仅", query.lower())
        and re.search(r"\b(?:metric|sales|traffic|daily)\w*\b|指标|销量|流量", query.lower())
        and re.search(r"\b(?:window|baseline|current)\w*\b|窗口|基线|当前", query.lower())
        and any(term in query.lower() for term in comparison_terms)
    )
    for clause in clauses:
        clause = clause.strip()
        if not clause or negated.match(clause):
            continue
        clause = negated_cause.sub("", clause)
        if any(term in clause for term in (*cause_terms, *record_terms)):
            return True
        matched_investigation_terms = [term for term in investigation_terms if term in clause]
        is_only_metric_comparison = exclusive_metric_comparison and all(
            term in comparison_terms for term in matched_investigation_terms
        )
        if matched_investigation_terms and not is_only_metric_comparison:
            return True
    return False


class DeterministicPlanner:
    name = "deterministic-investigation-v10"

    def decide(self, *, query: str, evidence: list[dict[str, Any]],
               history: list[dict[str, Any]], available_tools: list[str]) -> PlannerDecision:
        text = (query or "").lower()
        seen = {item.get("tool") or item.get("tool_name") for item in history}
        signals: list[str] = []
        for item in evidence:
            signals.extend(
                str(signal.get("metric") or signal.get("title") or "").lower()
                for signal in item.get("signals", [])
                if isinstance(signal, dict)
            )
            signals.append(str(item.get("metric", "")).lower())
        # Safety is an independent, immediate-review path.  Do this before the
        # normal review/text routing so a query such as “调查安全评论原因”
        # cannot spend additional investigation steps after a safety signal has
        # already been observed.
        if any("safety" in signal or "安全" in signal for signal in signals):
            return PlannerDecision(
                stop_reason="安全线索已分流给人工核查，调查图不继续扩大读取范围。",
                hypothesis="安全线索只能生成内部核查建议，不作自动经营变更。",
                counter_evidence_query="核对原文是否为否定、假设或与对象无关的安全表述。",
                confidence=0.95,
            )
        support_cues = (
            "support", "case", "ticket", "complaint", "客服", "工单", "售后", "包材", "包装",
            "adapter", "compatib", "charging", "damage", "smoke", "overheat", "burnt", "evidence",
            "过热", "发烫", "焦糊", "冒烟", "电气故障", "受损", "设备问题", "销量", "流量", "转化",
        )
        wants_support_text = any(word in text for word in support_cues)
        wants_review = any(word in text for word in ("评论", "review", "退货", "退款", "安全", "原因"))
        wants_text = wants_support_text or wants_review or any(
            word in text
            for word in (
                "原文", "证据", "record", "why", "listing", "return", "refund", "session", "conversion",
                "traffic", "sales", "metric", "cohort", "rating", "currency", "shipment", "requested", "received", "mature", "window",
                "source", "scope", "authorization", "authorized", "proposal", "asin", "sku", "permission",
                "complete", "missing", "stale", "compare", "change",
            )
        )
        if "metric_snapshot" not in seen and "metric_snapshot" in available_tools:
            return PlannerDecision(
                next_tool="metric_snapshot",
                hypothesis="先读取冻结指标快照，再根据已观察信号选择验证方向。",
                counter_evidence_query="指标信号是否能被评论、售后或运营事实反证？",
                confidence=0.6,
            )
        from .fact_followup import (
            BLOCKING_RULES,
            VERSION,
            focused_structured_followup,
            metric_workplan,
            order_lookup_requested,
            requested_metric_requests,
        )
        if order_lookup_requested(query) and "order_records" not in seen and "order_records" in available_tools:
            return PlannerDecision(next_tool="order_records", hypothesis="读取授权冻结订单及关联事件样本，不推断总体风险。")
        metric_focus = bool(requested_metric_requests(query)) or any(cue in text for cue in ("指标", "币种", "两窗", "metric", "currency", "window"))
        original_focus = any(cue in text for cue in ("评论", "原文", "包装", "过热", "packaging", "overheat", "review", "original", "support", "ticket", "客服", "工单"))
        # A missing prerequisite stops metric comparison before unrelated reads.
        # Explicit original-record inquiries still use their authorized text path.
        scoped_request = next((item.get("request_scope") for item in evidence if item.get("request_scope")), None)
        prerequisite = focused_structured_followup(query, evidence) if metric_focus and not original_focus and not (scoped_request or {}).get("status") == "explicit" else None
        workplan = metric_workplan(query, evidence)
        blocking_rules = BLOCKING_RULES
        if prerequisite:
            blocking = next((finding for finding in prerequisite.get("findings", []) if finding["rule_id"] in blocking_rules), None)
            prerequisite = {**prerequisite, **blocking} if blocking else None
        snapshots = [item for item in evidence if item.get("type") == "metric_snapshot"]
        if metric_focus and not original_focus and snapshots and all(item.get("status") == "missing" for item in snapshots):
            prerequisite = {"version": VERSION, "origin": "deterministic_tool_facts", "rule_id": "metric_snapshot_missing",
                            "next_step": "create_current_metric_snapshot", "instruction": "冻结指标快照缺失；先生成同一对象与来源版本的快照，暂停原因比较。",
                            "input_paths": ["metric_snapshot.status"], "citations": [],
                            "stop_condition": "stop_until_metric_snapshot_available", "findings": []}
        if workplan and not workplan["has_gaps"]:
            prerequisite = None
        if workplan and workplan["has_gaps"]:
            if workplan["available_metric_families"] or original_focus:
                prerequisite = None
            elif workplan["order_lookup_requested"] and not any(item.get("type") == "order_records" for item in evidence):
                prerequisite = {"version": VERSION, "origin": "deterministic_tool_facts", "rule_id": "order_detail_tool_unavailable",
                                "next_step": "provide_authorized_order_detail_tool_and_sources", "instruction": workplan["instruction"],
                                "stop_condition": "stop_until_order_detail_tool_available", "citations": [], "findings": [], "subtasks": workplan["subtasks"]}
            elif prerequisite is None:
                prerequisite = {"version": VERSION, "origin": "deterministic_tool_facts", "rule_id": "requested_metric_families_unavailable",
                                "next_step": "clarify_requested_window_or_provide_matching_snapshot" if any(task.get("window_status") == "mismatch" for task in workplan["subtasks"]) else "provide_complete_metric_family_snapshot", "instruction": workplan["instruction"],
                                "stop_condition": "stop_until_requested_metric_families_available", "citations": [], "findings": []}
        if prerequisite and workplan:
            prerequisite = {**prerequisite, "subtasks": workplan["subtasks"], "instruction": workplan["instruction"]}
        if prerequisite:
            from .business_schema import BUSINESS_FIELDS, BUSINESS_SCHEMA_VERSION
            contract = {"schema_version": BUSINESS_SCHEMA_VERSION, **{field: "unknown" for field in BUSINESS_FIELDS}}
            contract.update(expected_next_step=prerequisite["next_step"], expected_behavior="respect_observed_fact_boundary",
                            expected_stop_condition=prerequisite["stop_condition"],
                            allowed_conclusion="cited_metric_fact" if prerequisite["citations"] else "unknown",
                            forbidden_conclusion="causal_or_population_claim_without_evidence")
            return PlannerDecision(stop_reason=prerequisite["instruction"], hypothesis="比较所需结构化事实未满足，停止原因调查。",
                                   business_contract=contract, business_basis=prerequisite, confidence=None)
        if (workplan and not workplan["has_gaps"] and workplan["subtasks"]
                and all(task["mode"] == "daily" for task in workplan["subtasks"])
                and not _has_positive_additional_evidence_intent(query)):
            from .business_schema import BUSINESS_FIELDS, BUSINESS_SCHEMA_VERSION

            citations = [citation for task in workplan["subtasks"] for citation in task.get("citations", [])]
            contract = {"schema_version": BUSINESS_SCHEMA_VERSION, **{field: "unknown" for field in BUSINESS_FIELDS}}
            contract.update(
                expected_next_step="verify_frozen_requested_metric_facts",
                expected_behavior="respect_observed_fact_boundary",
                expected_stop_condition="stop_after_requested_frozen_facts",
                allowed_conclusion="cited_metric_fact" if citations else "unknown",
                forbidden_conclusion="causal_or_population_claim_without_evidence",
            )
            basis = {
                "version": VERSION,
                "origin": "deterministic_tool_facts",
                "subtasks": workplan["subtasks"],
                "instruction": workplan["instruction"],
                "stop_condition": "stop_after_requested_frozen_facts",
                "citations": citations,
            }
            return PlannerDecision(
                business_contract=contract,
                business_basis=basis,
                hypothesis="请求的销量与流量日指标已按冻结窗口核对；经营原因仍未知。",
                stop_reason="请求的销量与流量日指标冻结事实和窗口已核对；不据此判断经营原因。",
                confidence=0.75,
            )
        if wants_review and not wants_support_text and "review_claims" not in seen and "review_claims" in available_tools:
            return PlannerDecision(
                next_tool="review_claims",
                hypothesis="退货/退款或评论异常需要核对已接受的语义 claim。",
                counter_evidence_query="是否存在相反极性或无关样本，不能支持当前候选？",
                confidence=0.7,
            )
        if wants_text and "text_evidence" not in seen and "text_evidence" in available_tools:
            return PlannerDecision(
                next_tool="text_evidence",
                hypothesis="用冻结范围内的原文和售后记录验证候选解释。",
                counter_evidence_query="检索反证：是否存在相反反馈或其他主题？",
                confidence=0.65,
            )
        from .business_schema import BUSINESS_FIELDS, BUSINESS_SCHEMA_VERSION
        from .fact_followup import VERSION, focused_structured_followup, followup

        structured = None if original_focus and not metric_focus or (scoped_request or {}).get("status") == "explicit" else focused_structured_followup(query, evidence)
        next_step, statement, basis = followup(evidence, query=query)
        if structured:
            next_step, statement, basis = structured["next_step"], structured["instruction"], structured["rule_id"]
        if workplan and (workplan["has_gaps"] or workplan["order_lookup_requested"] or any(task["mode"] == "occurrence" for task in workplan["subtasks"])):
            structured = {**(structured or {}), "version": VERSION, "origin": "deterministic_tool_facts",
                          "subtasks": workplan["subtasks"], "instruction": workplan["instruction"],
                          "stop_condition": "stop_after_available_checks_with_unresolved_subtasks" if workplan["has_gaps"] else "stop_after_requested_frozen_facts",
                          "citations": (structured or {}).get("citations", [])}
            next_step = "complete_available_checks_and_request_missing_prerequisites" if workplan["has_gaps"] else "verify_frozen_requested_metric_facts"
        contract = {"schema_version": BUSINESS_SCHEMA_VERSION, **{field: "unknown" for field in BUSINESS_FIELDS}}
        contract["expected_next_step"] = next_step
        if structured:
            contract.update(expected_behavior="respect_observed_fact_boundary", expected_stop_condition=structured["stop_condition"],
                            allowed_conclusion="cited_metric_fact" if structured["citations"] else "unknown",
                            forbidden_conclusion="causal_or_population_claim_without_evidence")
        return PlannerDecision(
            business_contract=contract,
            business_basis=structured or {"version": VERSION, "origin": "deterministic_tool_facts", "reason": basis, "instruction": statement},
            stop_reason="当前可用工具已结束；仍有子任务缺源、缺工具或禁止总体推断，详情见分项结果。" if workplan and workplan["has_gaps"] else "当前授权范围内已完成可验证取证，剩余结论需要人工判断。",
            hypothesis="没有新的授权工具或反证方向，停止调查。",
            confidence=0.75,
        )


class ModelPlanner:
    """Adapter over the frozen JSON-schema planner integration."""

    def __init__(self):
        from app.semantic.factory import build_agent_planner

        self.planner = build_agent_planner()

    def decide(self, *, query: str, evidence: list[dict[str, Any]],
               history: list[dict[str, Any]], available_tools: list[str], request_context=None) -> PlannerDecision:
        from app.agent.state import InvestigationState

        observations = []
        for index, item in enumerate(evidence):
            # Returned review text is explicitly untrusted evidence; planner
            # instructions prohibit treating it as a command or authorization.
            observations.append({
                "tool_name": item.get("type", "evidence"),
                "status": item.get("status", "available"),
                "data": planner_evidence(item),
                "source_refs": [str(item.get("id", f"evidence-{index}"))],
            })
        observations = combine_text_observations(observations)
        if request_context and not any(item.get("type") == "text_retrieval" for item in evidence):
            observations.insert(0, {"tool_name": "authorized_scope", "status": "frozen_context",
                                   "data": request_context, "source_refs": []})
        state = InvestigationState(
            request_id="bounded-investigation-planner",
            user_query=query,
            frozen_scope=request_context or {},
            product_id=(request_context or {}).get("product_id") or (request_context or {}).get("asin"),
            period=(f"{request_context['start']}..{request_context['end']}" if request_context and request_context.get("start") and request_context.get("end") else None),
            observations=observations,
            hypotheses=[{"summary": item.get("hypothesis", ""), "counter_evidence_query": item.get("counter_evidence_query", ""),
                         "tool": item.get("tool"), "arguments": item.get("tool_args", {}), "stop_reason": item.get("stop_reason", "")} for item in history[-8:]],
            max_steps=max(len(history) + 1, 3),
            step_count=len(history),
            execution_mode="external_planner",
            planner_status="available",
        )


        try:
            result = self.planner.decide(state, tuple(available_tools))
        except ValueError as exc:
            from app.agent.planner import DuplicateTextEvidenceRequest
            from app.semantic.structured_response import StructuredResponseError
            gateway = self.planner.gateway
            receipt = {**getattr(gateway, "last_model_receipt", {}), "contract_error": str(exc)[:300]}
            code = "duplicate_text_evidence_request" if isinstance(exc, DuplicateTextEvidenceRequest) else "planner_contract_invalid" if str(exc).startswith("invalid planner response:") else "planner_request_preflight_failed"
            raise StructuredResponseError(code, receipt,
                                          getattr(gateway, "last_usage", {}), receipt.get("raw_response")) from exc
        if result.decision == "call_tool" and result.tool_name in available_tools:
            next_tool = result.tool_name or ""
            args = result.arguments
            stop_reason = ""
        else:
            next_tool, args = "", {}
            stop_reason = result.stop_reason or (
                "模型请求人工澄清，当前调查停止并保留决策记录。"
                if result.decision == "clarify"
                else "模型未给出白名单内工具，安全停止。"
            )
        usage = getattr(self.planner.gateway, "last_usage", {})
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        tokens = input_tokens + output_tokens
        input_rate = os.getenv("INVESTIGATION_INPUT_COST_PER_MILLION")
        output_rate = os.getenv("INVESTIGATION_OUTPUT_COST_PER_MILLION")
        cost_known = (input_rate is not None and output_rate is not None
                      and type(usage.get("input_tokens")) is int and type(usage.get("output_tokens")) is int
                      and min(usage["input_tokens"], usage["output_tokens"]) >= 0)
        cost = (
            str((Decimal(input_rate) * input_tokens + Decimal(output_rate) * output_tokens) / Decimal(1_000_000))
            if cost_known else "0"
        )
        conclusion = None
        try:
            parsed = json.loads(result.final_answer or "null")
            if isinstance(parsed, dict):
                conclusion = parsed
        except (ValueError, TypeError):
            pass
        return PlannerDecision(
            next_tool=next_tool,
            tool_args=args,
            hypothesis=result.decision_summary,
            counter_evidence_query=result.counter_evidence_query,
            stop_reason=stop_reason,
            clarification=", ".join(result.missing_fields) if result.decision == "clarify" else "",
            confidence=result.confidence,
            planner=getattr(self.planner.gateway, "provenance", "deepseek-agent-planner-v2"),
            execution_origin=getattr(self.planner.gateway, "execution_origin", "controlled_or_unknown"),
            model_receipt=getattr(self.planner.gateway, "last_model_receipt", None),
            final_answer=result.final_answer or "",
            conclusion=conclusion,
            business_contract=conclusion.get("business_contract") if isinstance(conclusion, dict) else None,
            tokens=tokens,
            cost=cost,
            cost_estimate_known=cost_known,
        )


def planner_evidence(item):
    """Keep evidence and scope intact; summarize transport audit metadata only.

    The durable decision still stores the complete original tool result. Receipt
    payloads, model usage and algorithm manifests are not business evidence.
    """
    if item.get("type") != "text_retrieval":
        return item
    keys = ("type", "query", "scope", "request_scope", "items",
            "limitations", "match_status", "retrieval_complete", "evidence_context")
    result = {key: item[key] for key in keys if key in item}
    if os.getenv("INVESTIGATION_PLANNER_PROTOCOL") in {"native-v2", "native-v3"} and isinstance(result.get("items"), list):
        # Durable evidence keeps every original field. Only transport IDs/rank
        # are omitted from the model view; original text/scope/citation IDs stay.
        result["items"] = [{key: value for key, value in entry.items()
                           if key in {"fact_id", "business_date", "title", "text", "match_kind", "evidence_contexts", "evidence_relation"}}
                          for entry in result["items"]]
    semantic = item.get("candidate_stage", {}).get("semantic_candidates", {})
    if semantic.get("status"):
        # Explain an unavailable index without treating transport metadata as
        # source evidence or transmitting the full ranked candidate audit.
        result["retrieval_diagnostics"] = {"semantic_index_status": semantic["status"]}
        if semantic.get("error"):
            result["retrieval_diagnostics"]["error"] = str(semantic["error"])[:160]
    if isinstance(result.get("scope"), dict):
        result["scope"] = {key: value for key, value in result["scope"].items() if value is not None}
    if isinstance(result.get("request_scope"), dict):
        result["request_scope"] = {key: value for key, value in result["request_scope"].items()
                                   if key in {"status", "start", "end"}}
    return result


def combine_text_observations(observations):
    """Share identical frozen scope and limitations without dropping searches."""
    texts = [obs for obs in observations if obs["data"].get("type") == "text_retrieval"]
    if len(texts) < 2:
        return observations
    def common_scope(obs):
        return {key: value for key, value in obs["data"].get("scope", {}).items() if key != "kind"}
    scope = common_scope(texts[0])
    requested = texts[0]["data"].get("request_scope")
    if any(common_scope(obs) != scope or obs["data"].get("request_scope") != requested for obs in texts):
        return observations
    items, limitations, searches, refs = [], [], [], []
    for obs in texts:
        data = obs["data"]
        for item in data.get("items", []):
            if item not in items:
                items.append(item)
        for limitation in data.get("limitations", []):
            if limitation not in limitations:
                limitations.append(limitation)
        searches.append({"kind": data.get("scope", {}).get("kind"),
                         "query": data.get("query"),
                         "retrieval_diagnostics": data.get("retrieval_diagnostics", {}),
                         "match_status": data.get("match_status"),
                         "retrieval_complete": data.get("retrieval_complete"),
                         "evidence_context": data.get("evidence_context")})
        refs.extend(obs["source_refs"])
    complete = all(search["retrieval_complete"] is True for search in searches)
    match_status = "no_hits" if complete and all(search["match_status"] == "no_hits" for search in searches) else "hits" if complete and items else "incomplete"
    combined = {"tool_name": "text_retrieval", "status": "available", "source_refs": refs,
                "data": {"type": "text_retrieval", "scope": scope, "request_scope": requested,
                         "items": items, "limitations": limitations, "searches": searches,
                         "retrieval_complete": complete, "match_status": match_status}}
    return [obs for obs in observations if obs not in texts] + [combined]


@lru_cache(maxsize=1)
def _external_planner(configuration_key=None):
    return ModelPlanner()


def planner_for(provider: str | None = None):
    selected = (provider or os.getenv("INVESTIGATION_PLANNER_PROVIDER", "deterministic")).strip().lower()
    if selected in {"", "deterministic", "test"}:
        return DeterministicPlanner()
    if selected in {"deepseek", "deepseek-v4-flash"}:
        enabled = {value.strip().lower() for value in os.getenv("INSIGHT_ENABLED_MODEL_PROVIDERS", "").split(",")}
        if "deepseek" not in enabled:
            raise PlannerUnavailable("DeepSeek 调查规划尚未获部署授权：先确认数据区域、密钥和费用预算。")
        try:
            from .execution_config import validate_external_planner_selection
            validate_external_planner_selection()
            from .execution_config import configuration_snapshot, content_digest
            return _external_planner(content_digest(configuration_snapshot('deepseek')))
        except Exception as exc:
            raise PlannerUnavailable(f"调查规划模型初始化失败：{type(exc).__name__}") from exc
    raise PlannerUnavailable("调查模型尚未配置经批准的供应商、区域与预算；请先完成模型验收。")
