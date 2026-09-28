from __future__ import annotations

from datetime import date
import calendar
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
import json
import os
import time
from uuid import uuid4
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from .repository import (
    demo_investigation,
    health,
    list_anomalies,
    list_products,
    metric_trend,
    overview,
    search_reviews,
)
from .services import AnomalyService, MetricService, MultiMetricService
from .tools import ReviewDetailTool, ReviewSearchTool, ToolRequest, ToolTimeRange
from .tools import MetricQueryTool, AnomalyDetectionTool, MultiMetricAnalysisTool, EvidenceGuardTool, ReviewSignalTool, ReviewSignalCompareTool, build_agent_registry
from .repositories.investigation_repository import InvestigationRepository
from .services.investigation_service import InvestigationService
from .agent.runtime_config import InvestigationRuntimeConfig
from .agent.router import TaskRouter, RouterInput, Route
from .agent.local_planner import DeterministicLocalPlanner
from .repositories.claim_store import ClaimStore
from .services.review_signal_service import ReviewSignalService
from .repositories.data_source_repository import DataSourceRepository


metric_service = MetricService()
anomaly_service = AnomalyService(metrics=metric_service)
multi_metric_service = MultiMetricService(metrics=metric_service, anomalies=anomaly_service)
review_search_tool = ReviewSearchTool()
review_detail_tool = ReviewDetailTool()
claim_store = ClaimStore()
review_signal_service = ReviewSignalService(claim_store)
data_sources = DataSourceRepository()


class _NoKeyPlanner:
    def decide(self, state, available_tools):
        from .agent.investigation import AgentDecision
        return AgentDecision(decision="stop", stop_reason="planner_unavailable", decision_summary="未配置模型密钥")


def _build_investigation_service():
    configured_mode = os.getenv("EXECUTION_MODE", "").strip().lower()
    if configured_mode == "deterministic_local" or (configured_mode != "external_planner" and not os.getenv("DEEPSEEK_API_KEY")):
        planner = DeterministicLocalPlanner()
        planner_mode, planner_status = "deterministic_local", planner.planner_status
    else:
        planner_mode, planner_status = "external_planner", "available"
        try:
            from .semantic.factory import build_agent_planner
            planner = build_agent_planner()
        except Exception:
            planner = _NoKeyPlanner()
            planner_status = "deferred_external_model"
    registry = build_agent_registry(MetricQueryTool(metric_service), AnomalyDetectionTool(anomaly_service), MultiMetricAnalysisTool(multi_metric_service), ReviewSignalTool(review_signal_service), ReviewSignalCompareTool(review_signal_service), review_search_tool, review_detail_tool, EvidenceGuardTool())
    service = InvestigationService(registry=registry, planner=planner, repository=InvestigationRepository())
    service.execution_mode, service.planner_status = planner_mode, planner_status
    return service


investigation_service = _build_investigation_service()
runtime_config = InvestigationRuntimeConfig()
task_router = TaskRouter()


class InvestigationCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_query: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    period: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    max_steps: int = Field(default=8, ge=1, le=20)
    parent_investigation_id: str | None = None


class ReviewSearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1)
    investigation_id: str | None = None
    product_id: str = Field(min_length=1)
    time_range: ToolTimeRange | None = None
    query: str = ""
    retrieval_backend: Literal["keyword", "hybrid", "qdrant", "qdrant_rrf"] = "keyword"
    rerank_mode: str = "auto"
    qdrant_collection: str | None = None
    min_rating: float | None = Field(default=None, ge=1, le=5)
    max_rating: float | None = Field(default=None, ge=1, le=5)
    top_k_final: int = Field(default=8, ge=1, le=20)


app = FastAPI(
    title="跨境商品经营洞察 API",
    version="0.1.0",
    description="Stage 5 确定性商品经营异常诊断能力",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/v1/health")
def get_health():
    payload = health()
    payload["data_sources"] = data_sources.health()
    planner_available = investigation_service.planner_status == "available"
    payload.update({
        "execution_mode": "external_planner" if planner_available else "deterministic_local",
        "planner_status": investigation_service.planner_status,
        "retrieval_default": "keyword",
        "qdrant_status": "experimental",
    })
    return payload


@app.get("/api/v1/data-sources")
def list_data_sources():
    return {"items": [item.model_dump(mode="json") for item in data_sources.list()], "database": str(data_sources.path)}


@app.post("/api/v1/data-sources/upload")
async def upload_data_source(kind: str = Query(...), file: UploadFile = File(...)):
    try:
        config = data_sources.import_csv(kind, file.filename or "upload.csv", await file.read())
        return config.model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/data-sources/sqlite/inspect")
def inspect_local_sqlite(path: str):
    try:
        return data_sources.inspect_sqlite(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/products")
def get_products():
    return {"items": list_products()}


@app.get("/api/v1/products/{product_id}/multi-metric-analysis")
def get_multi_metric_analysis(
    product_id: str,
    period: str,
    baseline_months: int = Query(3, ge=2, le=12),
):
    return _metric_call(multi_metric_service.analyze_product, product_id, period, baseline_months)


@app.get("/api/v1/overview")
def get_overview(product_id: str | None = None):
    return overview(product_id)


@app.get("/api/v1/metrics/trend")
def get_metric_trend(product_id: str, months: int = Query(18, ge=3, le=48)):
    products = {item["product_id"] for item in list_products()}
    if product_id not in products:
        raise HTTPException(status_code=404, detail="商品不存在")
    return metric_trend(product_id, months)


@app.get("/api/v1/metrics/query")
def query_metric(
    product_id: str,
    metric_name: str,
    start_date: date,
    end_date: date,
    granularity: Literal["day", "week", "month"] = "month",
):
    return _metric_call(
        metric_service.metric_query,
        product_id,
        metric_name,
        start_date,
        end_date,
        granularity,
    )


@app.get("/api/v1/metrics/aggregate")
def aggregate_metric(
    product_id: str,
    metric_name: str,
    start_date: date,
    end_date: date,
    granularity: Literal["day", "week", "month"] = "month",
):
    return _metric_call(
        metric_service.metric_aggregate,
        product_id,
        metric_name,
        start_date,
        end_date,
        granularity,
    )


@app.get("/api/v1/metrics/compare")
def compare_metric(
    product_id: str,
    metric_name: str,
    current_start: date,
    current_end: date,
    baseline_start: date,
    baseline_end: date,
    granularity: Literal["day", "week", "month"] = "month",
):
    return _metric_call(
        metric_service.metric_compare,
        product_id,
        metric_name,
        current_start,
        current_end,
        baseline_start,
        baseline_end,
        granularity,
    )


@app.get("/api/v1/anomalies")
def get_anomalies(product_id: str | None = None):
    return {"items": list_anomalies(product_id)}


@app.get("/api/v1/anomalies/detect")
def detect_anomaly(
    product_id: str,
    metric_name: str,
    current_start: date,
    current_end: date,
    baseline_start: date,
    baseline_end: date,
    granularity: Literal["day", "week", "month"] = "month",
    method: Literal["auto", "baseline_delta", "rolling_zscore", "robust_zscore", "change_point"] = "auto",
):
    return _metric_call(
        anomaly_service.detect,
        product_id,
        metric_name,
        current_start,
        current_end,
        baseline_start,
        baseline_end,
        granularity,
        method,
    )


@app.get("/api/v1/anomalies/{anomaly_id}")
def get_anomaly(anomaly_id: str):
    item = next((row for row in list_anomalies() if row["id"] == anomaly_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="异常不存在")
    return item


@app.get("/api/v1/reviews/topics")
def get_topics(product_id: str | None = None, period: str | None = None):
    items = claim_store.topics(product_id, period)
    if items:
        return {"items": items, "status": "ready", "product_id": product_id, "semantic_layer": "prediction_store", "ground_truth": False}
    return {
        "items": [],
        "status": "pending_offline_enrichment",
        "product_id": product_id,
        "warning": "业务 Topic 信号将在离线 Claim Store 构建后提供；运行时不读取 Golden 标签",
    }

@app.get("/api/v1/reviews/signals")
def get_review_signals(product_id: str, period: str):
    return {"items": review_signal_service.signals(product_id, period), "ground_truth": False}

@app.get("/api/v1/reviews/signals/compare")
def compare_review_signals(product_id: str, current_period: str, baseline_period: str):
    items = review_signal_service.compare(product_id, current_period, baseline_period)
    return {"product_id": product_id, "current_period": current_period, "baseline_period": baseline_period, "items": items, "insufficient_data": not items or all(x.get("insufficient_data") for x in items), "ground_truth": False}


@app.get("/api/v1/reviews/search")
def get_reviews(
    q: str = "",
    product_id: str | None = None,
    limit: int = Query(8, ge=1, le=20),
):
    return {
        "items": search_reviews(q, product_id, limit),
        "retrieval_backend": "keyword",
        "rerank_mode": "never",
        "embedding_version": None,
        "retrieval_version": "keyword-v1",
        "source_version": "amazon-reviews-2023-standardized-v1",
    }


@app.post("/api/v1/reviews/search")
def post_reviews(body: ReviewSearchBody):
    request = ToolRequest(
        request_id=body.request_id,
        investigation_id=body.investigation_id,
        product_id=body.product_id,
        time_range=body.time_range,
        params={
            "query": body.query,
            "retrieval_backend": body.retrieval_backend,
            "rerank_mode": body.rerank_mode,
            "qdrant_collection": body.qdrant_collection,
            "min_rating": body.min_rating,
            "max_rating": body.max_rating,
            "top_k_final": body.top_k_final,
        },
    )
    return review_search_tool.execute(request)


@app.get("/api/v1/reviews/{review_id}")
def get_review_detail(review_id: str):
    result = review_detail_tool.execute(
        ToolRequest(request_id=f"review-detail-{review_id}", product_id="runtime", params={"review_id": review_id})
    )
    if result.status == "error":
        raise HTTPException(status_code=404, detail="评论不存在")
    return result


@app.get("/api/v1/investigations/demo")
def get_demo_investigation(case_id: str | None = None):
    return demo_investigation(case_id)


@app.post("/api/v1/investigations", status_code=202)
def create_investigation(body: InvestigationCreateBody):
    if body.product_id and not metric_service.repository.product_exists(body.product_id):
        raise HTTPException(status_code=404, detail="商品不存在")
    parent = None
    if body.parent_investigation_id:
        parent = investigation_service.get(body.parent_investigation_id)
        if not parent: raise HTTPException(status_code=404, detail="父调查不存在")
        if parent.product_id != body.product_id: raise HTTPException(status_code=400, detail="后续调查必须使用同一商品")
    routed_query = body.user_query if not parent else f"基于调查 {parent.request_id} 的已验证证据继续诊断并结合评论解释：{body.user_query}"
    route = task_router.decide(RouterInput(user_query=routed_query, product_id=body.product_id, period=body.period))
    if route.route in {Route.CLARIFICATION, Route.UNSUPPORTED}:
        return {"route": route.route.value, "task_type": route.task_type, "reason": route.reason, "missing_fields": route.missing_fields}
    if route.route == Route.TOOL:
        y, m = [int(x) for x in (body.period or "2000-01").split("-")]
        start, end = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
        metric = "refund_rate" if any(x in body.user_query.casefold() for x in ("退款", "refund")) else "order_count"
        result = investigation_service.registry.execute("metric_query_tool", ToolRequest(request_id=f"l0-{uuid4().hex}", product_id=body.product_id, time_range=ToolTimeRange(start=start, end=end), params={"metric_name": metric}))
        return {"route": route.route.value, "task_type": route.task_type, "status": result.status, "result": result.model_dump(mode="json"), "reason": route.reason}
    if route.route == Route.WORKFLOW:
        if route.task_type == "detect_business_anomaly":
            y, m = [int(x) for x in (body.period or "2000-01").split("-")]
            current_start = date(y, m, 1); current_end = date(y, m, calendar.monthrange(y, m)[1])
            idx = y * 12 + m - 1 - 3; by, bm = divmod(idx, 12); bm += 1
            baseline_start = date(by, bm, 1); baseline_end = date(y if m > 1 else y - 1, m - 1 if m > 1 else 12, calendar.monthrange(y if m > 1 else y - 1, m - 1 if m > 1 else 12)[1])
            data = anomaly_service.detect(body.product_id, "refund_rate", current_start, current_end, baseline_start, baseline_end, "month", "auto")
        else:
            data = metric_trend(body.product_id, 18)
        return {"route": route.route.value, "task_type": route.task_type, "status": "completed", "result": data, "reason": route.reason}
    state = investigation_service.create(user_query=routed_query, product_id=body.product_id, period=body.period or "", max_steps=body.max_steps, route=route.route.value, route_reason=route.reason, execution_mode=getattr(investigation_service, "execution_mode", "external_planner"), planner_status=getattr(investigation_service, "planner_status", "unknown"), parent_investigation_id=body.parent_investigation_id)
    return {"investigation_id": state.request_id, "status": state.status, "product_id": state.product_id, "period": state.period, "execution_mode": state.execution_mode, "planner_status": state.planner_status}


@app.get("/api/v1/investigations/{investigation_id}")
def get_investigation(investigation_id: str):
    state = investigation_service.get(investigation_id)
    if not state: raise HTTPException(status_code=404, detail="调查不存在")
    return state.model_dump(mode="json")


@app.get("/api/v1/investigations/{investigation_id}/events")
def get_investigation_events(investigation_id: str, after_sequence: int = Query(0, ge=0)):
    if not investigation_service.get(investigation_id): raise HTTPException(status_code=404, detail="调查不存在")
    return {"items": investigation_service.events(investigation_id, after_sequence)}


@app.get("/api/v1/investigations/{investigation_id}/stream")
def stream_investigation(investigation_id: str, after_sequence: int = Query(0, ge=0)):
    if not investigation_service.get(investigation_id): raise HTTPException(status_code=404, detail="调查不存在")
    def generate():
        seq = after_sequence
        idle = 0
        while idle < 120:
            events = investigation_service.events(investigation_id, seq)
            if events:
                for event in events:
                    seq = event["sequence"]
                    yield f"id: {seq}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                idle = 0
            else:
                state = investigation_service.get(investigation_id)
                if state and state.status in {"completed", "stopped", "failed", "timeout", "planner_unavailable", "cancelled"}:
                    break
                idle += 1
                time.sleep(runtime_config.poll_interval_seconds)
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _metric_call(function, product_id: str, *args):
    if not metric_service.repository.product_exists(product_id):
        raise HTTPException(status_code=404, detail="商品不存在")
    try:
        return function(product_id, *args)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
