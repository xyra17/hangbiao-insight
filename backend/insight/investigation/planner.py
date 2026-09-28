"""Planner adapter boundary for bounded investigation decisions.

The deterministic planner is intentionally explicit and testable.  A real
provider can implement the same structured interface once its region, budget
and data-transfer approval is configured; no provider response is allowed to
execute a tool without the domain policy check in ``policy.py``.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class PlannerDecision:
    next_tool: str = ""
    tool_args: dict[str, Any] | None = None
    hypothesis: str = ""
    counter_evidence_query: str = ""
    stop_reason: str = ""
    confidence: float | None = None
    planner: str = "deterministic-investigation-v1"
    tokens: int = 0
    cost: str = "0"
    cost_estimate_known: bool = True

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["tool_args"] = value["tool_args"] or {}
        return value


class PlannerUnavailable(RuntimeError):
    pass


class DeterministicPlanner:
    name = "deterministic-investigation-v1"

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
        wants_review = any(word in text for word in ("评论", "review", "退货", "退款", "安全", "原因"))
        wants_text = wants_review or any(word in text for word in ("原文", "评论", "原因", "why", "review"))
        if "metric_snapshot" not in seen and "metric_snapshot" in available_tools:
            return PlannerDecision(
                next_tool="metric_snapshot",
                hypothesis="先读取冻结指标快照，再根据已观察信号选择验证方向。",
                counter_evidence_query="指标信号是否能被评论、售后或运营事实反证？",
                confidence=0.6,
            )
        if wants_review and "review_claims" not in seen and "review_claims" in available_tools:
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
        if evidence and any("safety" in signal or "安全" in signal for signal in signals):
            return PlannerDecision(
                stop_reason="安全线索已分流给人工核查，调查图不继续扩大读取范围。",
                hypothesis="安全线索只能生成内部核查建议，不作自动经营变更。",
                confidence=0.95,
            )
        return PlannerDecision(
            stop_reason="当前授权范围内已完成可验证取证，剩余结论需要人工判断。",
            hypothesis="没有新的授权工具或反证方向，停止调查。",
            confidence=0.75,
        )


class ModelPlanner:
    """Adapter over the frozen JSON-schema planner integration."""

    def __init__(self):
        from app.semantic.factory import build_agent_planner

        self.planner = build_agent_planner()

    def decide(self, *, query: str, evidence: list[dict[str, Any]],
               history: list[dict[str, Any]], available_tools: list[str]) -> PlannerDecision:
        from app.agent.state import InvestigationState

        observations = []
        for index, item in enumerate(evidence):
            # Returned review text is explicitly untrusted evidence; planner
            # instructions prohibit treating it as a command or authorization.
            observations.append({
                "tool_name": item.get("type", "evidence"),
                "status": item.get("status", "available"),
                "data": item,
                "source_refs": [str(item.get("id", f"evidence-{index}"))],
            })
        state = InvestigationState(
            request_id="bounded-investigation-planner",
            user_query=query,
            observations=observations,
            hypotheses=[{"summary": item.get("hypothesis", ""), "evidence": item.get("evidence", [])} for item in history],
            max_steps=max(len(history) + 1, 3),
            step_count=len(history),
            execution_mode="external_planner",
            planner_status="available",
        )


        result = self.planner.decide(state, tuple(available_tools))
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
        cost_known = input_rate is not None and output_rate is not None
        cost = (
            str((Decimal(input_rate) * input_tokens + Decimal(output_rate) * output_tokens) / Decimal(1_000_000))
            if cost_known else "0"
        )
        return PlannerDecision(
            next_tool=next_tool,
            tool_args=args,
            hypothesis=result.decision_summary,
            counter_evidence_query=result.counter_evidence_query,
            stop_reason=stop_reason,
            confidence=result.confidence,
            planner="deepseek-agent-planner-v1",
            tokens=tokens,
            cost=cost,
            cost_estimate_known=cost_known,
        )


@lru_cache(maxsize=1)
def _external_planner():
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
            return _external_planner()
        except Exception as exc:
            raise PlannerUnavailable(f"调查规划模型初始化失败：{type(exc).__name__}") from exc
    raise PlannerUnavailable("调查模型尚未配置经批准的供应商、区域与预算；请先完成模型验收。")
