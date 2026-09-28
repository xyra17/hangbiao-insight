from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..tools.contracts import ToolRequest
from ..tools.registry import ToolRegistry
from .state import InvestigationState, TraceEvent


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str | None = None
    tool_name: str | None = None
    arguments: dict = Field(default_factory=dict)
    decision_summary: str = ""
    target_hypothesis_id: str | None = None
    stop_reason: str | None = None
    final_answer: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    missing_fields: list[str] = Field(default_factory=list)
    # legacy aliases retained for existing callers
    action: str | None = None
    params: dict | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def normalize_legacy(self):
        if self.decision is None:
            self.decision = "call_tool" if self.action == "tool" else (self.action or "stop")
        if not self.arguments and self.params:
            self.arguments = dict(self.params)
        if not self.decision_summary and self.reason:
            self.decision_summary = self.reason
        if self.decision == "call_tool" and (not self.tool_name or self.arguments is None):
            raise ValueError("call_tool requires tool_name and arguments")
        if self.decision == "stop" and not self.stop_reason:
            raise ValueError("stop requires stop_reason")
        if self.decision == "clarify" and not self.missing_fields:
            raise ValueError("clarify requires missing_fields")
        if self.decision not in {"call_tool", "stop", "clarify"}:
            raise ValueError("invalid decision")
        return self

    @property
    def params_legacy(self) -> dict:
        return self.arguments


class Planner(Protocol):
    def decide(self, state: InvestigationState, available_tools: tuple[str, ...]) -> AgentDecision: ...


class AgentInvestigation:
    """Stateful planner/tool loop; the planner owns next-action selection."""

    def __init__(self, *, planner: Planner, tools: ToolRegistry) -> None:
        self.planner = planner
        self.tools = tools

    def run(self, state: InvestigationState) -> InvestigationState:
        state.status = "running"
        state.trace.append(TraceEvent(event_type="investigation_started"))
        while state.status == "running":
            if not state.enforce_budget():
                break
            decision = self.planner.decide(state, self.tools.names())
            state.next_action = decision.model_dump(mode="json")
            state.trace.append(TraceEvent(event_type="planner_decision", detail=state.next_action))
            if decision.decision == "stop":
                state.stop(decision.stop_reason or "agent_decided_no_further_action")
                break
            if decision.decision != "call_tool" or not decision.tool_name:
                state.stop("invalid_planner_decision", failed=True)
                break
            try:
                tool_params = dict(decision.arguments)
                if decision.tool_name == "evidence_guard_tool":
                    tool_params["allowed_source_refs"] = state.allowed_source_refs()
                result = self.tools.execute(
                    decision.tool_name,
                    ToolRequest(request_id=state.request_id, investigation_id=state.request_id,
                                product_id=state.product_id or "", params=tool_params),
                )
            except Exception as exc:
                state.record_tool_failure(tool_name=decision.tool_name, params=decision.arguments, error=exc)
                if state.step_count >= state.max_steps:
                    state.stop("max_steps_reached")
                continue
            state.record_tool_result(tool_name=decision.tool_name, params=tool_params, result=result)
        return state
