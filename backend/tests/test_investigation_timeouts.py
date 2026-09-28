import time
from pathlib import Path

from app.agent.investigation import AgentDecision
from app.agent.runtime_config import InvestigationRuntimeConfig
from app.repositories.investigation_repository import InvestigationRepository
from app.services.investigation_service import InvestigationService
from app.tools.contracts import ToolRequest, ToolResult
from app.tools.registry import ToolRegistry, ToolSpec


def _service(tmp_path: Path, planner, execute=lambda req: ToolResult(tool_name="slow", status="success", data={}, source_version="t", latency_ms=1, trace_id="t"), **kw):
    reg = ToolRegistry(); reg.register(ToolSpec(name="slow", description="", input_schema=ToolRequest, output_schema=ToolResult, execute=execute))
    return InvestigationService(registry=reg, planner=planner, repository=InvestigationRepository(tmp_path / "i.db"), config=InvestigationRuntimeConfig(**kw))


def _wait(svc, iid):
    for _ in range(100):
        s=svc.get(iid)
        if s.status != "running" and s.status != "pending": return s
        time.sleep(.01)
    return svc.get(iid)


def test_planner_timeout_reaches_terminal(tmp_path):
    class Planner:
        def decide(self, *a): time.sleep(.2)
    s=_service(tmp_path, Planner(), planner_timeout_seconds=.02, investigation_timeout_seconds=1)
    st=_wait(s,s.create(user_query="x",product_id="p",period="2020-01").request_id)
    assert st.status == "timeout"; assert any(e["event_type"] == "planner_timeout" for e in s.events(st.request_id))


def test_tool_timeout_reaches_terminal(tmp_path):
    class Planner:
        def decide(self, *a): return AgentDecision(decision="call_tool",tool_name="slow",arguments={},decision_summary="x")
    def slow(*a): time.sleep(.2)
    s=_service(tmp_path, Planner(), execute=slow, tool_timeout_seconds=.02, investigation_timeout_seconds=1)
    st=_wait(s,s.create(user_query="x",product_id="p",period="2020-01").request_id)
    assert st.status == "timeout"; assert any(e["event_type"] == "tool_timeout" for e in s.events(st.request_id))


def test_total_timeout_and_sse_events_are_recoverable(tmp_path):
    class Planner:
        def decide(self, *a): time.sleep(.2)
    s=_service(tmp_path, Planner(), planner_timeout_seconds=1, investigation_timeout_seconds=.02)
    st=_wait(s,s.create(user_query="x",product_id="p",period="2020-01").request_id)
    events=s.events(st.request_id, 1)
    assert st.status == "timeout"; assert all(a["sequence"] < b["sequence"] for a,b in zip(events,events[1:]))


def test_final_answer_and_confidence_persist(tmp_path):
    class Planner:
        def decide(self, *a): return AgentDecision(decision="stop", stop_reason="abstain", final_answer="证据不足，暂不归因", confidence=.2)
    s=_service(tmp_path, Planner(), investigation_timeout_seconds=1)
    st=_wait(s,s.create(user_query="x",product_id="p",period="2020-01").request_id)
    restored=s.get(st.request_id)
    assert restored.final_answer == "证据不足，暂不归因" and restored.confidence == .2


def test_final_answer_sanitizes_overclaim_and_abstain_template():
    from app.agent.final_answer import sanitize_final_answer
    text, changed = sanitize_final_answer("评论导致退款上升", insufficient_evidence=False)
    assert changed and "不能证明" in text
    text, _ = sanitize_final_answer("工具失败", insufficient_evidence=True)
    assert "评论证据不足以解释" in text and "退款原因编码" in text


def test_tool_failure_observation_has_structured_fields(tmp_path):
    class Planner:
        def decide(self, *a): return AgentDecision(decision="call_tool", tool_name="slow", arguments={}, decision_summary="x")
    def broken(*a): raise RuntimeError("down")
    s=_service(tmp_path, Planner(), execute=broken, investigation_timeout_seconds=1)
    st=_wait(s,s.create(user_query="x",product_id="p",period="2020-01",max_steps=1).request_id)
    obs=st.observations[0]; assert obs["status"] == "error" and obs["error_type"] == "RuntimeError" and "retryable" in obs
