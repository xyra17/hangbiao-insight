from __future__ import annotations

from .investigation import AgentDecision
from .state import InvestigationState


class DeterministicLocalPlanner:
    """Offline planner for local Core MVP runs; it never calls a model."""

    planner_status = "local_deterministic"

    def decide(self, state: InvestigationState, available_tools: tuple[str, ...]) -> AgentDecision:
        observed = {str(item.get("tool_name")): item for item in state.observations}
        if "anomaly_detection_tool" not in observed and "anomaly_detection_tool" in available_tools:
            return AgentDecision(
                decision="call_tool",
                tool_name="anomaly_detection_tool",
                arguments={"metric_name": "refund_rate"},
                decision_summary="本地确定性模式先核对退款率异常。",
            )
        if "review_signal_tool" not in observed and "review_signal_tool" in available_tools:
            return AgentDecision(
                decision="call_tool",
                tool_name="review_signal_tool",
                arguments={"period": state.period or ""},
                decision_summary="读取月度 Topic×Polarity 评论信号。",
            )
        retrieval = observed.get("review_search_tool")
        if retrieval is None and "review_search_tool" in available_tools:
            return AgentDecision(
                decision="call_tool",
                tool_name="review_search_tool",
                arguments={"query": state.user_query, "retrieval_backend": "keyword", "top_k_final": 5},
                decision_summary="使用 Keyword 检索可回查评论。",
            )
        refs = list((retrieval or {}).get("source_refs", []))
        detail = observed.get("review_detail_tool")
        if refs and detail is None and "review_detail_tool" in available_tools:
            return AgentDecision(
                decision="call_tool",
                tool_name="review_detail_tool",
                arguments={"review_id": str(refs[0])},
                decision_summary="回查检索到的 Review 原文。",
            )
        if (
            detail
            and detail.get("status") == "success"
            and "evidence_guard_tool" in available_tools
            and "evidence_guard_tool" not in observed
        ):
            review = (detail.get("data") or {}).get("review") or {}
            quote = str(review.get("text") or review.get("title") or "").strip()
            if quote:
                return AgentDecision(
                    decision="call_tool",
                    tool_name="evidence_guard_tool",
                    arguments={
                        "claim": "评论内容与当前经营异常存在相关性信号，但不足以证明因果关系。",
                        "evidence": [
                            {
                                "source_type": "review",
                                "source_id": str(detail.get("source_refs", [""])[0]),
                                "quote": quote,
                            }
                        ],
                    },
                    decision_summary="仅使用已回查的评论原文进行 Evidence Guard。",
                )
        if not refs:
            return AgentDecision(
                decision="stop",
                stop_reason="no_explanation",
                final_answer="当前评论证据不足以解释本次经营异常，系统不做评论归因。建议检查退款原因编码、售后/退货记录、批次质量、价格促销、广告流量、库存和数据质量。",
                confidence=0.0,
                decision_summary="本地检索没有返回可回查评论。",
            )
        return AgentDecision(
            decision="stop",
            stop_reason="sufficient_support",
            final_answer="当前评论显示与经营异常相关的用户侧信号，但相关性不等于因果关系，仍需结合退款、售后和批次数据验证。",
            confidence=0.4,
            decision_summary="本地证据链完成，保留因果边界。",
        )
