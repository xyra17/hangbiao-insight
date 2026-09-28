from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from .contracts import ToolRequest, ToolResult


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: float = 0.0


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    execute: Callable[[ToolRequest], ToolResult]
    timeout_seconds: float = 30.0
    retry_policy: RetryPolicy = RetryPolicy()
    trace_metadata: dict[str, str] | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def spec(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def execute(self, name: str, request: ToolRequest) -> ToolResult:
        return self.spec(name).execute(request)


def build_review_registry(search_tool, detail_tool, evidence_guard_tool=None) -> ToolRegistry:
    registry = ToolRegistry()
    tools = [
        (search_tool, "按商品、时间、关键词和评分条件检索业务评论"),
        (detail_tool, "按 review_id 回读标准化业务评论原文"),
    ]
    if evidence_guard_tool:
        tools.append((evidence_guard_tool, "验证证据来源、引用和因果边界"))
    for tool, description in tools:
        registry.register(
            ToolSpec(
                name=tool.name,
                description=description,
                input_schema=ToolRequest,
                output_schema=ToolResult,
                execute=tool.execute,
                retry_policy=RetryPolicy(max_attempts=1),
                trace_metadata={"layer": "review_repository"},
            )
        )
    return registry


def build_agent_registry(*tools) -> ToolRegistry:
    descriptions = {
        "metric_query_tool": "查询单商品指定时间范围的经营指标",
        "anomaly_detection_tool": "对单商品单指标执行确定性异常检测",
        "multi_metric_analysis_tool": "联合分析订单与退款经营状态",
        "review_search_tool": "按商品、时间、关键词和评分条件检索业务评论",
        "review_detail_tool": "按 review_id 回读标准化业务评论原文",
        "evidence_guard_tool": "验证证据来源、引用和因果边界",
        "review_signal_tool": "查询商品月度 Topic × Polarity 评论信号",
        "review_signal_compare_tool": "比较当前与基线评论信号",
    }
    registry = ToolRegistry()
    for tool in tools:
        registry.register(
            ToolSpec(
                name=tool.name,
                description=descriptions[tool.name],
                input_schema=ToolRequest,
                output_schema=ToolResult,
                execute=tool.execute,
                retry_policy=RetryPolicy(max_attempts=1),
                trace_metadata={"layer": "agent_capability"},
            )
        )
    return registry
