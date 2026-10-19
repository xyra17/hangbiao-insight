from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Route(StrEnum):
    TOOL = "tool"
    WORKFLOW = "workflow"
    AGENT = "agent_investigation"
    CLARIFICATION = "clarification"
    UNSUPPORTED = "unsupported"


class RouterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_query: str = Field(min_length=1)
    product_id: str | None = None
    period: str | None = None


class RouterResult(BaseModel):
    route: Route
    task_type: str
    product_id: str | None
    period: str | None
    missing_fields: list[str]
    reason: str


class TaskRouter:
    _unsupported = ("广告", "acos", "roas", "库存", "流量", "sessions", "促销", "价格")

    def decide(self, request: RouterInput) -> RouterResult:
        query = request.user_query.casefold()
        missing = []
        if not request.product_id:
            missing.append("product_id")
        if not request.period:
            missing.append("period")
        if missing:
            return RouterResult(
                route=Route.CLARIFICATION,
                task_type="missing_context",
                product_id=request.product_id,
                period=request.period,
                missing_fields=missing,
                reason="缺少商品或时间范围，不能安全查询",
            )
        if any(term in query for term in self._unsupported):
            return RouterResult(
                route=Route.UNSUPPORTED,
                task_type="pending_data_source",
                product_id=request.product_id,
                period=request.period,
                missing_fields=[],
                reason="请求依赖当前尚未接入的经营数据源",
            )
        if any(term in query for term in ("为什么", "原因", "解释", "诊断", "评论证据", "能否解释")):
            route, task, reason = (
                Route.AGENT,
                "diagnose_business_anomaly",
                "需要结合多个工具形成并验证竞争假设",
            )
        elif any(term in query for term in ("是否异常", "检测异常", "判断异常")):
            route, task, reason = (
                Route.WORKFLOW,
                "detect_business_anomaly",
                "异常判断路径确定，可由确定性工作流完成",
            )
        elif any(term in query for term in ("趋势", "近6个月", "近 6 个月", "对比", "比较")):
            route, task, reason = Route.WORKFLOW, "compare_metric_trend", "需要固定的查询与聚合步骤"
        else:
            route, task, reason = Route.TOOL, "query_metric", "单次确定性工具查询即可回答"
        return RouterResult(
            route=route,
            task_type=task,
            product_id=request.product_id,
            period=request.period,
            missing_fields=[],
            reason=reason,
        )
