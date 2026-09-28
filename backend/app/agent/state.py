from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..tools.contracts import ToolResult


class TraceEvent(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ActionRecord(BaseModel):
    tool_name: str
    params: dict[str, Any]
    status: str
    trace_id: str
    error: str | None = None


class InvestigationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    user_query: str
    parent_investigation_id: str | None = None
    product_id: str | None = None
    period: str | None = None
    task_type: str = "diagnose_business_anomaly"
    status: Literal["pending", "running", "completed", "stopped", "failed", "timeout", "planner_unavailable", "cancelled"] = "pending"
    observations: list[dict[str, Any]] = Field(default_factory=list)
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    next_action: dict[str, Any] | None = None
    stop_reason: str | None = None
    final_answer: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    route: str | None = None
    route_reason: str | None = None
    execution_mode: str = "external_planner"
    planner_status: str = "unknown"
    step_count: int = 0
    max_steps: int = Field(default=8, ge=1, le=20)
    trace: list[TraceEvent] = Field(default_factory=list)

    def record_tool_result(self, *, tool_name: str, params: dict[str, Any], result: ToolResult) -> None:
        self.step_count += 1
        error = "; ".join(result.warnings) if result.status == "error" else None
        self.actions.append(ActionRecord(tool_name=tool_name, params=params, status=result.status, trace_id=result.trace_id, error=error))
        self.observations.append({"tool_name": tool_name, "status": result.status, "data": result.data, "source_refs": result.evidence_refs})
        evidence_status = result.data.get("evidence_status")
        if evidence_status == "verified":
            self.evidence.extend(result.data.get("validated_evidence", []))
        elif evidence_status == "conflict":
            self.conflicts.append({"tool_name": tool_name, "validation": result.data})
        self.trace.append(TraceEvent(event_type="tool_completed", detail={"tool_name": tool_name, "status": result.status, "trace_id": result.trace_id}))

    def record_tool_failure(self, *, tool_name: str, params: dict[str, Any], error: Exception) -> None:
        self.step_count += 1
        trace_id = f"failed-{self.request_id}-{self.step_count}"
        self.actions.append(ActionRecord(tool_name=tool_name, params=params, status="error", trace_id=trace_id, error=type(error).__name__))
        self.observations.append({"tool_name": tool_name, "status": "error", "error_type": type(error).__name__, "retryable": False, "message": str(error)[:240], "source_refs": []})
        self.trace.append(TraceEvent(event_type="tool_failed", detail={"tool_name": tool_name, "error_type": type(error).__name__, "trace_id": trace_id}))

    def stop(self, reason: str, *, failed: bool = False) -> None:
        if reason == "timeout": self.status = "timeout"
        elif reason == "planner_unavailable": self.status = "planner_unavailable"
        else: self.status = "failed" if failed else "stopped"
        self.stop_reason = reason
        self.next_action = None
        self.trace.append(TraceEvent(event_type="investigation_stopped", detail={"reason": reason}))

    def enforce_budget(self) -> bool:
        if self.step_count >= self.max_steps:
            self.stop("max_steps_reached")
            return False
        return True

    def allowed_source_refs(self) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        for observation in self.observations:
            tool_name = str(observation.get("tool_name", ""))
            if tool_name.startswith("review_"):
                source_type = "review"
            elif "metric" in tool_name:
                source_type = "metric"
            elif "anomaly" in tool_name:
                source_type = "anomaly"
            else:
                source_type = "system"
            refs.extend(
                {"source_type": source_type, "source_id": str(source_id)}
                for source_id in observation.get("source_refs", [])
            )
        return list({(item["source_type"], item["source_id"]): item for item in refs}.values())
