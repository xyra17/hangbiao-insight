from __future__ import annotations

from datetime import date
from pydantic import BaseModel, ConfigDict, Field

from .investigation import AgentDecision
from .state import InvestigationState
from ..tools.contracts import ToolRequest, ToolTimeRange
from ..tools.registry import ToolRegistry


class PlannerValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    valid: bool
    request: ToolRequest | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PlannerDecisionValidator:
    """安全边界：Planner 只能选择已注册工具，并继承路由确定的商品与时间范围。"""
    def validate(self, decision: AgentDecision, state: InvestigationState, registry: ToolRegistry) -> PlannerValidationResult:
        if decision.decision != "call_tool":
            return PlannerValidationResult(valid=True)
        if not decision.tool_name or decision.tool_name not in registry.names():
            return PlannerValidationResult(valid=False, errors=["unknown_tool"])
        args = dict(decision.arguments)
        if state.step_count >= state.max_steps:
            return PlannerValidationResult(valid=False, errors=["max_steps_reached"])
        import json
        fingerprint = json.dumps(args, sort_keys=True, default=str)
        if any(action.tool_name == decision.tool_name and json.dumps(action.params, sort_keys=True, default=str) == fingerprint for action in state.actions):
            return PlannerValidationResult(valid=False, errors=["duplicate_tool_call"])
        forbidden = {"source_ref", "source_refs", "allowed_source_refs", "verified_evidence"}
        forged = forbidden.intersection(args)
        warnings: list[str] = []
        if forged:
            if decision.tool_name != "evidence_guard_tool":
                return PlannerValidationResult(valid=False, errors=["forbidden_source_argument"])
            for key in forged: args.pop(key, None)
            warnings.append("forged_source_arguments_removed")
        if "product_id" in args and str(args["product_id"]) != str(state.product_id):
            return PlannerValidationResult(valid=False, errors=["product_scope_violation"])
        if "period" in args and state.period and str(args["period"]) != str(state.period):
            return PlannerValidationResult(valid=False, errors=["period_scope_violation"])
        if decision.tool_name == "evidence_guard_tool":
            args.pop("allowed_source_refs", None)
        tr = None
        if "time_range" in args:
            raw = args.pop("time_range")
            if not isinstance(raw, dict):
                return PlannerValidationResult(valid=False, errors=["invalid_time_range"])
            try:
                tr = ToolTimeRange.model_validate(raw)
            except Exception:
                return PlannerValidationResult(valid=False, errors=["invalid_time_range"])
        if tr is None and (args.get("period") or state.period):
            period = str(args.get("period") or state.period)
            try:
                y, m = [int(x) for x in period.split("-")[:2]]
                import calendar
                tr = ToolTimeRange(start=date(y, m, 1), end=date(y, m, calendar.monthrange(y, m)[1]), granularity="month")
            except Exception:
                return PlannerValidationResult(valid=False, errors=["invalid_period"])
        params = args.pop("params", args)
        if not isinstance(params, dict):
            return PlannerValidationResult(valid=False, errors=["invalid_arguments"])
        if decision.tool_name == "evidence_guard_tool":
            if "claim" not in params and isinstance(params.get("evidence_claim"), str):
                params["claim"] = params.pop("evidence_claim")
                warnings.append("evidence_claim_normalized")
            if isinstance(params.get("evidence"), dict):
                raw = params["evidence"]; refs = raw.get("source_refs", [])
                data = raw.get("data", {}) if isinstance(raw.get("data", {}), dict) else {}
                quote = str(data.get("reason") or data.get("summary_zh") or "经营指标检测结果")
                params["evidence"] = [{"source_type": "anomaly" if any(str(x).startswith("anomaly:") for x in refs) else "metric", "source_id": str(x), "quote": quote} for x in refs]
                warnings.append("evidence_object_normalized")
            if not isinstance(params.get("evidence"), list) or not params.get("evidence"):
                candidates = []
                for obs in state.observations:
                    for ref in obs.get("source_refs", []):
                        ref = str(ref); source_type = "review" if len(ref) == 40 else ("anomaly" if ref.startswith("anomaly:") else "metric")
                        quote = ""
                        if source_type == "review":
                            for item in obs.get("data", {}).get("items", []):
                                if str(item.get("review_id")) == ref: quote = str(item.get("text") or item.get("title") or ""); break
                        else:
                            quote = str(obs.get("data", {}).get("reason") or obs.get("data", {}).get("summary_zh") or "经营指标检测结果")
                        if quote: candidates.append({"source_type": source_type, "source_id": ref, "quote": quote})
                if candidates:
                    params["evidence"] = candidates; warnings.append("evidence_from_observed_sources")
            if not isinstance(params.get("claim"), str) or not params["claim"].strip():
                return PlannerValidationResult(valid=False, errors=["evidence_claim_required"])
            evidence = params.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                return PlannerValidationResult(valid=False, errors=["evidence_required"])
            for item in evidence:
                if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip() for k in ("source_type", "source_id", "quote")):
                    return PlannerValidationResult(valid=False, errors=["invalid_evidence_item"])
        if "metric_name" not in params and isinstance(params.get("metrics"), list) and params["metrics"]:
            params["metric_name"] = params["metrics"][0]
        if decision.tool_name == "anomaly_detection_tool":
            if "metric_name" not in params:
                params["metric_name"] = "refund_rate"
            if "baseline_start" not in params or "baseline_end" not in params:
                from datetime import date as _date
                import calendar
                month_start = tr.start
                idx = month_start.year * 12 + month_start.month - 1 - 3
                by, bm = divmod(idx, 12); bm += 1
                params.setdefault("baseline_start", _date(by, bm, 1).isoformat())
                params.setdefault("baseline_end", _date(month_start.year, month_start.month - 1 if month_start.month > 1 else 12, calendar.monthrange(month_start.year if month_start.month > 1 else month_start.year - 1, month_start.month - 1 if month_start.month > 1 else 12)[1]).isoformat())
        req = ToolRequest(request_id=state.request_id, investigation_id=state.request_id,
                          product_id=state.product_id or "", time_range=tr, params=params)
        return PlannerValidationResult(valid=True, request=req, warnings=warnings)
