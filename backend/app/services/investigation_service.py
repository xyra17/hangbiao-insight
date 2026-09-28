from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from uuid import uuid4

from ..agent.investigation import AgentDecision, AgentInvestigation, Planner
from ..agent.state import InvestigationState
from ..agent.validator import PlannerDecisionValidator
from ..repositories.investigation_repository import InvestigationRepository
from ..tools.registry import ToolRegistry
from ..agent.final_answer import sanitize_final_answer
from ..agent.runtime_config import InvestigationRuntimeConfig


class InvestigationService:
    def __init__(self, *, registry: ToolRegistry, planner: Planner, repository: InvestigationRepository | None = None, config: InvestigationRuntimeConfig | None = None) -> None:
        self.registry = registry
        self.planner = planner
        self.repository = repository or InvestigationRepository()
        self.validator = PlannerDecisionValidator()
        self.config = config or InvestigationRuntimeConfig()

    def create(self, *, user_query: str, product_id: str, period: str, max_steps: int = 8, route: str | None = None, route_reason: str | None = None, execution_mode: str = "external_planner", planner_status: str = "unknown", parent_investigation_id: str | None = None) -> InvestigationState:
        state = InvestigationState(request_id=f"inv-{uuid4().hex}", user_query=user_query, product_id=product_id, period=period, max_steps=max_steps, route=route, route_reason=route_reason, execution_mode=execution_mode, planner_status=planner_status, parent_investigation_id=parent_investigation_id)
        self.repository.save_state(state)
        self.repository.append_event(state.request_id, "investigation_created", {"product_id": product_id, "period": period})
        threading.Thread(target=self._run, args=(state.request_id,), daemon=True).start()
        return state

    def _run(self, investigation_id: str) -> None:
        state = self.repository.get(investigation_id)
        if not state:
            return
        self.repository.append_event(investigation_id, "investigation_started")
        try:
            state.status = "running"
            started_at = time.monotonic()
            consecutive_failures = 0
            while state.status == "running":
                if time.monotonic() - started_at >= self.config.investigation_timeout_seconds:
                    state.stop("timeout"); self.repository.save_state(state); self.repository.append_event(investigation_id, "investigation_timeout", {"reason": "investigation_timeout"}); return
                if not state.enforce_budget():
                    break
                # Deterministic investigation policy: anomaly -> ReviewSignal before retrieval.
                if state.observations and state.observations[-1].get("tool_name") == "anomaly_detection_tool" and "review_signal_tool" in self.registry.names() and not any(o.get("tool_name") == "review_signal_tool" for o in state.observations):
                    decision = AgentDecision(decision="call_tool", tool_name="review_signal_tool", arguments={"period": state.period}, decision_summary="先读取该月 Topic×Polarity 评论信号，再决定检索方向。")
                else:
                    executor = ThreadPoolExecutor(max_workers=1)
                    future = executor.submit(self.planner.decide, state, self.registry.names())
                    remaining = max(0.01, self.config.investigation_timeout_seconds - (time.monotonic() - started_at))
                    try: decision = future.result(timeout=min(self.config.planner_timeout_seconds, remaining))
                    except FutureTimeout:
                        future.cancel(); executor.shutdown(wait=False, cancel_futures=True)
                        state.stop("timeout"); self.repository.save_state(state); self.repository.append_event(investigation_id, "planner_timeout", {"reason": "planner_timeout"}); return
                    finally:
                        executor.shutdown(wait=False, cancel_futures=True)
                self.repository.append_event(investigation_id, "planner_decision", {"decision": decision.model_dump(mode="json")})
                observed_tools={o.get("tool_name") for o in state.observations}
                # Hard runtime order: once the signal step has completed, a
                # retrieval observation is required before detail/guard.
                signal_ok = any(o.get("tool_name") in {"review_signal_tool", "review_signal_compare_tool"} and o.get("status") == "success" for o in state.observations)
                retrieval_ok = any(o.get("tool_name") == "review_search_tool" and o.get("status") in {"success", "partial"} and o.get("source_refs") for o in state.observations)
                detail_ok = any(o.get("tool_name") == "review_detail_tool" and o.get("status") == "success" and o.get("source_refs") for o in state.observations)
                if signal_ok and not retrieval_ok and decision.tool_name not in {"review_search_tool", "review_signal_tool", "review_signal_compare_tool"}:
                    state.step_count += 1
                    state.observations.append({"tool_name":"planner_policy","status":"error","error_type":"retrieval_required_after_signal","retryable":True,"source_refs":[]})
                    self.repository.append_event(investigation_id,"planner_policy_blocked",{"reason":"retrieval_required_after_signal"})
                    continue
                if decision.tool_name == "review_detail_tool" and not retrieval_ok:
                    state.step_count += 1; state.observations.append({"tool_name":"planner_policy","status":"error","error_type":"retrieval_required","retryable":True,"source_refs":[]}); self.repository.append_event(investigation_id,"planner_policy_blocked",{"reason":"retrieval_required"}); continue
                if decision.tool_name == "evidence_guard_tool" and not detail_ok:
                    state.step_count += 1; state.observations.append({"tool_name":"planner_policy","status":"error","error_type":"detail_required","retryable":True,"source_refs":[]}); self.repository.append_event(investigation_id,"planner_policy_blocked",{"reason":"detail_required"}); continue
                validation = self.validator.validate(decision, state, self.registry)
                if not validation.valid:
                    self.repository.append_event(investigation_id, "planner_validation_failed", {"errors": validation.errors})
                    state.observations.append({"tool_name": "planner_validator", "status": "error", "error_type": "validation_failed", "errors": validation.errors, "retryable": True, "source_refs": []})
                    consecutive_failures += 1
                    if consecutive_failures >= 2:
                        state.final_answer = "调查决策连续未通过安全校验，暂不生成诊断。"; state.confidence = 0.0
                        state.stop("planner_validation_failed", failed=True); break
                    self.repository.save_state(state)
                    continue
                if validation.warnings:
                    self.repository.append_event(investigation_id, "planner_arguments_sanitized", {"warnings": validation.warnings})
                if decision.decision == "stop":
                    state.final_answer, sanitized = sanitize_final_answer(decision.final_answer, insufficient_evidence=decision.stop_reason in {"insufficient_evidence", "no_explanation"})
                    state.confidence = decision.confidence if decision.confidence is not None else 0.0
                    if sanitized: self.repository.append_event(investigation_id, "final_answer_sanitized", {"reason": decision.stop_reason or "overclaim"})
                    state.stop(decision.stop_reason or "agent_decided_no_further_action"); break
                if decision.decision == "clarify":
                    state.final_answer = "缺少必要调查信息，暂无法生成诊断。"; state.confidence = 0.0
                    state.stop("clarification_required"); break
                req = validation.request
                if req is None:
                    state.final_answer = "调查决策无效，暂不生成诊断。"; state.confidence = 0.0
                    state.stop("invalid_planner_decision", failed=True); break
                if decision.tool_name == "evidence_guard_tool":
                    req.params["allowed_source_refs"] = state.allowed_source_refs()
                self.repository.append_event(investigation_id, "tool_started", {"tool_name": decision.tool_name})
                try:
                    executor = ThreadPoolExecutor(max_workers=1)
                    future = executor.submit(self.registry.execute, decision.tool_name or "", req)
                    try: result = future.result(timeout=self.config.tool_timeout_seconds)
                    except FutureTimeout:
                        future.cancel(); state.record_tool_failure(tool_name=decision.tool_name or "", params=req.params, error=TimeoutError("tool_timeout")); self.repository.save_state(state); self.repository.append_event(investigation_id, "tool_timeout", {"tool_name": decision.tool_name}); state.stop("timeout"); self.repository.save_state(state); self.repository.append_event(investigation_id, "investigation_timeout", {"reason": "tool_timeout"}); return
                    finally:
                        executor.shutdown(wait=False, cancel_futures=True)
                    state.record_tool_result(tool_name=decision.tool_name or "", params=req.params, result=result)
                    consecutive_failures = 0
                    self.repository.append_event(investigation_id, "tool_completed", {"tool_name": result.tool_name, "status": result.status})
                    if result.data.get("evidence_status") == "verified":
                        self.repository.append_event(investigation_id, "evidence_verified", {"evidence_count": len(result.data.get("validated_evidence", [])), "source_refs": result.evidence_refs, "trace_id": result.trace_id})
                except Exception as exc:
                    consecutive_failures += 1
                    state.record_tool_failure(tool_name=decision.tool_name or "", params=req.params, error=exc)
                    self.repository.append_event(investigation_id, "tool_failed", {"tool_name": decision.tool_name, "error_type": type(exc).__name__})
                    if consecutive_failures >= 2:
                        state.final_answer = "当前评论证据不足以解释本次经营异常，系统不做评论归因。建议进一步检查退款原因编码、售后/退货记录、批次质量、价格促销、广告流量、库存和数据质量。"; state.confidence = 0.0
                        state.stop("insufficient_evidence"); self.repository.save_state(state); self.repository.append_event(investigation_id, "investigation_stopped", {"reason": "insufficient_evidence"}); return
                self.repository.save_state(state)
            if state.status == "running": state.status = "completed"
            self.repository.save_state(state)
            self.repository.append_event(investigation_id, "investigation_completed", {"status": state.status, "stop_reason": state.stop_reason})
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "required" in str(exc).lower():
                state.stop("planner_unavailable"); event_type = "planner_unavailable"
            else:
                state.stop("investigation_failed", failed=True); event_type = "investigation_failed"
            self.repository.save_state(state)
            self.repository.append_event(investigation_id, event_type, {"error_type": type(exc).__name__})

    def get(self, investigation_id: str): return self.repository.get(investigation_id)
    def events(self, investigation_id: str, after_sequence: int = 0): return self.repository.events(investigation_id, after_sequence)
