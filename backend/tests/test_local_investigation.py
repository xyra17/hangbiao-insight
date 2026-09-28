from app.agent.local_planner import DeterministicLocalPlanner
from app.agent.state import InvestigationState


def test_local_planner_emits_ordered_local_actions_without_model_calls():
    planner = DeterministicLocalPlanner()
    state = InvestigationState(request_id="local-1", user_query="为什么退款率上升，结合评论解释", product_id="B07P9S6R9Y", period="2020-06")
    available = ("anomaly_detection_tool", "review_signal_tool", "review_search_tool", "review_detail_tool", "evidence_guard_tool")

    first = planner.decide(state, available)
    assert first.tool_name == "anomaly_detection_tool"
    state.observations.append({"tool_name": "anomaly_detection_tool", "status": "success", "source_refs": ["anomaly:p"]})
    assert planner.decide(state, available).tool_name == "review_signal_tool"
    state.observations.append({"tool_name": "review_signal_tool", "status": "success", "source_refs": []})
    assert planner.decide(state, available).tool_name == "review_search_tool"
    state.observations.append({"tool_name": "review_search_tool", "status": "success", "source_refs": ["r1"]})
    assert planner.decide(state, available).tool_name == "review_detail_tool"
    state.observations.append({"tool_name": "review_detail_tool", "status": "success", "source_refs": ["r1"], "data": {"review": {"review_id": "r1", "text": "charges were unexpected"}}})
    decision = planner.decide(state, available)
    assert decision.tool_name == "evidence_guard_tool"
    assert decision.arguments["evidence"][0]["source_id"] == "r1"
