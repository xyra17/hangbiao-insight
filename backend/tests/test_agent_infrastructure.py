from app.agent.investigation import AgentDecision, AgentInvestigation
from app.agent.router import Route, RouterInput, TaskRouter
from app.agent.state import InvestigationState
from app.tools import (
    AnomalyDetectionTool,
    EvidenceGuardTool,
    MetricQueryTool,
    MultiMetricAnalysisTool,
    ReviewDetailTool,
    ReviewSearchTool,
    ToolRegistry,
    ToolRequest,
    ToolResult,
    ToolSpec,
    ToolTimeRange,
    build_agent_registry,
)


def result(name, data, refs=(), status="success"):
    return ToolResult(tool_name=name, status=status, data=data, evidence_refs=list(refs), source_version="test-v1", latency_ms=1, trace_id=f"trace-{name}")


def test_investigation_state_serializes_records_failures_and_stops_at_budget():
    state = InvestigationState(request_id="i1", user_query="诊断", product_id="p1", period="2022-06", max_steps=1)
    state.record_tool_result(tool_name="metric", params={}, result=result("metric", {"value": 1}, status="error"))
    assert state.actions[0].status == "error"
    assert state.observations[0]["data"]["value"] == 1
    assert state.enforce_budget() is False
    assert state.stop_reason == "max_steps_reached"
    assert InvestigationState.model_validate_json(state.model_dump_json()).request_id == "i1"

    conflict = result("evidence_guard_tool", {"evidence_status": "conflict", "validated_evidence": []})
    other = InvestigationState(request_id="i-conflict", user_query="诊断")
    other.record_tool_result(tool_name="evidence_guard_tool", params={}, result=conflict)
    assert len(other.conflicts) == 1

    sourced = InvestigationState(request_id="i-sources", user_query="诊断")
    sourced.record_tool_result(tool_name="review_search_tool", params={}, result=result("review_search_tool", {}, ["r1", "r1"]))
    sourced.record_tool_result(tool_name="metric_query_tool", params={}, result=result("metric_query_tool", {}, ["m1"]))
    assert sourced.allowed_source_refs() == [
        {"source_type": "review", "source_id": "r1"},
        {"source_type": "metric", "source_id": "m1"},
    ]


def test_router_covers_tool_workflow_agent_clarification_and_unsupported():
    router = TaskRouter()
    base = {"product_id": "p1", "period": "2022-06"}
    assert router.decide(RouterInput(user_query="查询订单量", **base)).route == Route.TOOL
    assert router.decide(RouterInput(user_query="判断是否异常", **base)).route == Route.WORKFLOW
    assert router.decide(RouterInput(user_query="为什么退款上涨，结合评论解释", **base)).route == Route.AGENT
    assert router.decide(RouterInput(user_query="查询订单量", product_id="p1")).route == Route.CLARIFICATION
    assert router.decide(RouterInput(user_query="分析广告 ROAS", **base)).route == Route.UNSUPPORTED


class DynamicPlanner:
    def decide(self, state, available_tools):
        if not state.observations:
            return AgentDecision(action="tool", tool_name="metric", reason="先确认经营事实")
        if len(state.observations) == 1 and state.observations[0]["data"]["refund_up"]:
            return AgentDecision(action="tool", tool_name="reviews", params={"query": "stopped"}, reason="退款异常后转查评论")
        return AgentDecision(action="stop", reason="评论不足", stop_reason="insufficient_evidence")


def test_agent_loop_dynamically_changes_tools_and_keeps_auditable_trace():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="metric", description="metric", input_schema=ToolRequest, output_schema=ToolResult, execute=lambda _: result("metric", {"refund_up": True}, ["metric-1"])))
    registry.register(ToolSpec(name="reviews", description="reviews", input_schema=ToolRequest, output_schema=ToolResult, execute=lambda _: result("reviews", {"items": []}, [])))
    state = AgentInvestigation(planner=DynamicPlanner(), tools=registry).run(
        InvestigationState(request_id="i2", user_query="为什么退款上涨", product_id="p1", period="2022-06")
    )
    assert [action.tool_name for action in state.actions] == ["metric", "reviews"]
    assert state.stop_reason == "insufficient_evidence"
    assert state.status == "stopped"
    assert [event.event_type for event in state.trace].count("planner_decision") == 3


def test_agent_injects_only_tool_observed_sources_into_evidence_guard():
    class EvidencePlanner:
        def decide(self, state, available_tools):
            if not state.observations:
                return AgentDecision(action="tool", tool_name="review_search_tool", reason="search")
            if len(state.observations) == 1:
                return AgentDecision(
                    action="tool",
                    tool_name="evidence_guard_tool",
                    params={"allowed_source_refs": [{"source_type": "review", "source_id": "forged"}]},
                    reason="validate",
                )
            return AgentDecision(action="stop", reason="done", stop_reason="sufficient_support")

    seen = []
    registry = ToolRegistry()
    registry.register(ToolSpec(name="review_search_tool", description="search", input_schema=ToolRequest, output_schema=ToolResult, execute=lambda _: result("review_search_tool", {}, ["r1"])))

    def evidence(request):
        seen.extend(request.params["allowed_source_refs"])
        return result("evidence_guard_tool", {"evidence_status": "verified", "validated_evidence": []}, ["r1"])

    registry.register(ToolSpec(name="evidence_guard_tool", description="guard", input_schema=ToolRequest, output_schema=ToolResult, execute=evidence))
    AgentInvestigation(planner=EvidencePlanner(), tools=registry).run(
        InvestigationState(request_id="i-evidence", user_query="诊断", product_id="p1", period="2022-06")
    )
    assert seen == [{"source_type": "review", "source_id": "r1"}]


def test_agent_records_tool_failure_and_stops_safely_at_limit():
    class FailingPlanner:
        def decide(self, state, available_tools):
            return AgentDecision(action="tool", tool_name="broken", reason="try")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="broken", description="broken", input_schema=ToolRequest, output_schema=ToolResult, execute=lambda _: (_ for _ in ()).throw(RuntimeError("down"))))
    state = AgentInvestigation(planner=FailingPlanner(), tools=registry).run(
        InvestigationState(request_id="i3", user_query="诊断", product_id="p1", period="2022-06", max_steps=2)
    )
    assert state.step_count == 2
    assert state.stop_reason == "max_steps_reached"
    assert len(state.actions) == 2
    assert all(action.status == "error" for action in state.actions)
    assert [event.event_type for event in state.trace].count("tool_failed") == 2


def test_metric_adapters_and_full_p0_registry_use_standard_contract():
    registry = build_agent_registry(
        MetricQueryTool(),
        AnomalyDetectionTool(),
        MultiMetricAnalysisTool(),
        ReviewSearchTool(),
        ReviewDetailTool(),
        EvidenceGuardTool(),
    )
    assert set(registry.names()) == {
        "metric_query_tool",
        "anomaly_detection_tool",
        "multi_metric_analysis_tool",
        "review_search_tool",
        "review_detail_tool",
        "evidence_guard_tool",
    }
    metric = registry.execute(
        "metric_query_tool",
        ToolRequest(
            request_id="metric-1",
            product_id="B07P9S6R9Y",
            time_range=ToolTimeRange(start="2020-06-01", end="2020-06-30"),
            params={"metric_name": "refund_rate"},
        ),
    )
    multi = registry.execute(
        "multi_metric_analysis_tool",
        ToolRequest(request_id="multi-1", product_id="B07P9S6R9Y", params={"period": "2022-02"}),
    )
    assert metric.tool_name == "metric_query_tool"
    assert metric.evidence_refs[0].startswith("metric:B07P9S6R9Y")
    assert multi.data["pattern"] == "growth_with_refund_risk"
