from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolTimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    granularity: Literal["day", "week", "month"] = "month"


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1)
    investigation_id: str | None = None
    product_id: str = Field(min_length=1)
    marketplace: str = "Amazon US"
    time_range: ToolTimeRange | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_name: str
    status: Literal["success", "partial", "error"]
    data: dict[str, Any]
    evidence_refs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_version: str
    latency_ms: int = Field(ge=0)
    trace_id: str
