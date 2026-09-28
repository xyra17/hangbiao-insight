from __future__ import annotations

from datetime import date
from time import perf_counter
from uuid import uuid4

from ..services import AnomalyService, MetricService, MultiMetricService
from .contracts import ToolRequest, ToolResult


def _result(name: str, data: dict, source_ref: str, started: float) -> ToolResult:
    return ToolResult(
        tool_name=name,
        status="success",
        data=data,
        evidence_refs=[source_ref],
        source_version=str(data.get("metric_version") or data.get("engine_version") or "business-metric-v1.0"),
        latency_ms=round((perf_counter() - started) * 1000),
        trace_id=f"TR-{uuid4().hex}",
    )


class MetricQueryTool:
    name = "metric_query_tool"

    def __init__(self, service: MetricService | None = None) -> None:
        self.service = service or MetricService()

    def execute(self, request: ToolRequest) -> ToolResult:
        if not request.time_range:
            raise ValueError("metric_query_tool requires time_range")
        started = perf_counter()
        metric = str(request.params["metric_name"])
        data = self.service.metric_query(request.product_id, metric, request.time_range.start, request.time_range.end, request.time_range.granularity)
        ref = f"metric:{request.product_id}:{metric}:{request.time_range.start}:{request.time_range.end}"
        return _result(self.name, data, ref, started)


class AnomalyDetectionTool:
    name = "anomaly_detection_tool"

    def __init__(self, service: AnomalyService | None = None) -> None:
        self.service = service or AnomalyService()

    def execute(self, request: ToolRequest) -> ToolResult:
        if not request.time_range:
            raise ValueError("anomaly_detection_tool requires current time_range")
        started = perf_counter()
        metric = str(request.params["metric_name"])
        baseline_start = date.fromisoformat(str(request.params["baseline_start"]))
        baseline_end = date.fromisoformat(str(request.params["baseline_end"]))
        data = self.service.detect(
            request.product_id,
            metric,
            request.time_range.start,
            request.time_range.end,
            baseline_start,
            baseline_end,
            request.time_range.granularity,
            str(request.params.get("method", "auto")),
        )
        ref = f"anomaly:{request.product_id}:{metric}:{request.time_range.start}:{request.time_range.end}"
        return _result(self.name, data, ref, started)


class MultiMetricAnalysisTool:
    name = "multi_metric_analysis_tool"

    def __init__(self, service: MultiMetricService | None = None) -> None:
        self.service = service or MultiMetricService()

    def execute(self, request: ToolRequest) -> ToolResult:
        started = perf_counter()
        period = str(request.params.get("period") or (request.time_range.start.strftime("%Y-%m") if request.time_range else ""))
        data = self.service.analyze_product(request.product_id, period, int(request.params.get("baseline_months", 3)))
        ref = f"multi-metric:{request.product_id}:{period}"
        return _result(self.name, data, ref, started)
