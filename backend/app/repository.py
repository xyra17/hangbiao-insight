from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .repositories.review_repository import ReviewRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET_ROOT = PROJECT_ROOT / "dataset"

PRODUCT_NAMES = {
    "B01G0X56YU": "WixGear 车载手机支架",
    "B07H2V5YLH": "Ailun 屏幕保护膜",
    "B07P9S6R9Y": "Yootech 无线充电器",
    "B0B1TCPH54": "JETech 充电线",
    "B0BYJ8ZVWH": "AILKIN USB 墙充",
}

PRODUCT_IMAGES = {
    "B01G0X56YU": "车载支架",
    "B07H2V5YLH": "屏幕保护",
    "B07P9S6R9Y": "无线充电",
    "B0B1TCPH54": "充电线缆",
    "B0BYJ8ZVWH": "墙式充电",
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _number(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=1)
def product_rows() -> list[dict[str, str]]:
    path = DATASET_ROOT / "data" / "01_标准化评论数据" / "评论数据集汇总_v1.csv"
    return _read_csv(path)


@lru_cache(maxsize=1)
def monthly_business_rows() -> list[dict[str, str]]:
    path = DATASET_ROOT / "outputs" / "08_经营指标" / "01_经营指标月表_v1.csv"
    return _read_csv(path)


@lru_cache(maxsize=1)
def monthly_review_rows() -> list[dict[str, str]]:
    path = DATASET_ROOT / "outputs" / "02_评论探索分析" / "02_评论月度指标.csv"
    return _read_csv(path)


@lru_cache(maxsize=1)
def diagnosis_rows() -> list[dict[str, str]]:
    path = DATASET_ROOT / "outputs" / "09_诊断案例" / "01_诊断案例候选_v1.csv"
    return _read_csv(path)


def list_products() -> list[dict[str, Any]]:
    anomaly_count = Counter(row.get("product_id") for row in diagnosis_rows())
    products = []
    for row in product_rows():
        product_id = row.get("sku", "")
        products.append(
            {
                "product_id": product_id,
                "name": PRODUCT_NAMES.get(product_id, row.get("product_name") or product_id),
                "category": PRODUCT_IMAGES.get(product_id, "手机配件"),
                "marketplace": "Amazon US",
                "currency": "USD",
                "review_count": int(_number(row.get("review_count"))),
                "avg_rating": round(_number(row.get("avg_rating")), 2),
                "first_review": row.get("first_review"),
                "last_review": row.get("last_review"),
                "anomaly_count": anomaly_count.get(product_id, 0),
                "status": "attention" if anomaly_count.get(product_id, 0) else "healthy",
                "source": "Amazon Reviews 2023",
            }
        )
    return products


def metric_trend(product_id: str, months: int = 18) -> dict[str, Any]:
    business = [row for row in monthly_business_rows() if row.get("product_id") == product_id]
    reviews = [row for row in monthly_review_rows() if (row.get("product_id") or row.get("sku")) == product_id]
    review_by_month = {str(row.get("month", ""))[:7]: row for row in reviews}
    business.sort(key=lambda row: row.get("month", ""))
    business = business[-max(3, min(months, 48)) :]
    points = []
    for row in business:
        month = str(row.get("month", ""))[:7]
        review = review_by_month.get(month, {})
        points.append(
            {
                "month": month,
                "orders": int(round(_number(row.get("order_count")))),
                "refund_rate": round(_number(row.get("refund_rate")) * 100, 2),
                "rating": round(_number(review.get("avg_rating")), 2) or None,
                "reviews": int(round(_number(review.get("review_count")))),
                "is_injected": str(row.get("is_injected", "")).lower() == "true",
                "scenario_type": row.get("scenario_type") or None,
            }
        )
    return {
        "product_id": product_id,
        "product_name": PRODUCT_NAMES.get(product_id, product_id),
        "granularity": "month",
        "source_version": "business-metric-v1.0 + Amazon Reviews 2023",
        "points": points,
    }


def list_anomalies(product_id: str | None = None) -> list[dict[str, Any]]:
    anomalies = []
    for index, row in enumerate(diagnosis_rows(), start=1):
        if product_id and row.get("product_id") != product_id:
            continue
        order_change = _number(row.get("order_change_pct")) * 100
        refund_delta = _number(row.get("refund_rate_delta")) * 100
        rating_delta = _number(row.get("rating_delta"))
        severity_score = max(abs(order_change) / 20, abs(refund_delta) / 1.5, abs(rating_delta) / 0.15)
        severity = "high" if severity_score >= 1.8 else "medium" if severity_score >= 1 else "low"
        signals = []
        if abs(order_change) >= 0.01:
            signals.append({"label": "订单量", "change": round(order_change, 1), "unit": "%"})
        if abs(refund_delta) >= 0.01:
            signals.append({"label": "退款率", "change": round(refund_delta, 2), "unit": "pp"})
        if abs(rating_delta) >= 0.001:
            signals.append({"label": "评分", "change": round(rating_delta, 2), "unit": ""})
        anomalies.append(
            {
                "id": row.get("case_id") or f"ANOM-{index:03d}",
                "scenario_id": row.get("scenario_id"),
                "product_id": row.get("product_id"),
                "product_name": PRODUCT_NAMES.get(row.get("product_id", ""), row.get("product_name")),
                "month": str(row.get("current_month", ""))[:7],
                "severity": severity,
                "status": "pending" if "candidate" in str(row.get("case_status")) else "diagnosed",
                "relation": row.get("expected_review_relation"),
                "signals": signals,
                "summary": _anomaly_summary(signals),
                "source": "synthetic_evaluation_fixture",
            }
        )
    return sorted(anomalies, key=lambda item: (item["month"], item["severity"]), reverse=True)


def _anomaly_summary(signals: list[dict[str, Any]]) -> str:
    if not signals:
        return "检测到经营指标偏离基线"
    parts = [f'{signal["label"]}{"上升" if signal["change"] > 0 else "下降"}{abs(signal["change"]):g}{signal["unit"]}' for signal in signals[:2]]
    return "，".join(parts)


def overview(product_id: str | None = None) -> dict[str, Any]:
    products = list_products()
    selected = product_id or (products[0]["product_id"] if products else "")
    trend = metric_trend(selected)
    points = trend["points"]
    latest = points[-1] if points else {"orders": 0, "refund_rate": 0, "rating": None, "reviews": 0}
    previous = points[-2] if len(points) > 1 else latest
    return {
        "selected_product_id": selected,
        "products": products,
        "kpis": [
            _kpi("订单量", latest["orders"], "单", previous["orders"], "orders", True),
            _kpi("退款率", latest["refund_rate"], "%", previous["refund_rate"], "refund", False),
            _kpi("平均评分", latest["rating"] or 0, "分", previous["rating"] or latest["rating"] or 0, "rating", True),
            _kpi("评论量", latest["reviews"], "条", previous["reviews"], "reviews", True),
        ],
        "coverage": [
            {"name": "销售额 GMV", "status": "pending", "hint": "待接订单金额数据"},
            {"name": "转化率 CVR", "status": "pending", "hint": "待接 Sessions 与订单"},
            {"name": "广告 ROAS", "status": "pending", "hint": "待接广告平台"},
            {"name": "库存可售天数", "status": "pending", "hint": "待接库存数据"},
        ],
        "trend": trend,
        "anomalies": list_anomalies(selected),
        "updated_at": datetime.now().astimezone().isoformat(timespec="minutes"),
    }


def _kpi(label: str, value: float, unit: str, previous: float, key: str, higher_is_better: bool) -> dict[str, Any]:
    delta = 0 if previous == 0 else (value - previous) / abs(previous) * 100
    good = delta >= 0 if higher_is_better else delta <= 0
    return {
        "key": key,
        "label": label,
        "value": round(value, 2),
        "unit": unit,
        "delta": round(delta, 1),
        "direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
        "tone": "positive" if good else "negative",
    }


def search_reviews(query: str = "", product_id: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
    return ReviewRepository().search(query=query, product_id=product_id, limit=limit)


def demo_investigation(case_id: str | None = None) -> dict[str, Any]:
    anomalies = list_anomalies()
    anomaly = next((item for item in anomalies if item["id"] == case_id), anomalies[0] if anomalies else None)
    if not anomaly:
        return {"status": "empty", "steps": [], "evidence": []}
    evidence = search_reviews(product_id=anomaly["product_id"], limit=4)
    relation = anomaly.get("relation")
    no_explanation = relation == "no_explanation"
    return {
        "investigation_id": f'DEMO-{anomaly["id"]}',
        "execution_mode": "L2 Agent Investigation",
        "route_reason": "问题包含多指标异常，需要根据中间证据动态选择调查方向",
        "status": "completed",
        "anomaly": anomaly,
        "steps": [
            {"stage": "route", "title": "确认调查模式", "detail": "多个经营指标存在变化，进入开放式调查", "state": "done"},
            {"stage": "metric", "title": "核对经营异常", "detail": anomaly["summary"], "state": "done"},
            {"stage": "hypothesis", "title": "形成竞争假设", "detail": "优先检查功能可靠性、兼容适配与评论无法解释三条路径", "state": "done"},
            {"stage": "retrieval", "title": "检索评论证据", "detail": f'回查 {len(evidence)} 条代表性评论并核验原文', "state": "done"},
            {"stage": "evaluate", "title": "评估支持与冲突", "detail": "评论仅作为同期相关信号，不作因果证明", "state": "done"},
            {"stage": "stop", "title": "停止调查", "detail": "现有评论证据不足，建议继续检查广告、价格、库存与活动" if no_explanation else "已有足够相关信号，可形成带边界的诊断结论", "state": "stop"},
        ],
        "diagnosis": {
            "title": "评论暂不能充分解释本次经营异常" if no_explanation else "评论信号与经营异常存在同期相关",
            "summary": "系统未发现足够稳定的评论主题变化来解释全部指标，保留未解释维度。" if no_explanation else "部分评论主题与异常窗口同步变化，但仍不能据此断言因果关系。",
            "confidence": "证据不足" if no_explanation else "中等",
            "stop_reason": "insufficient_evidence" if no_explanation else "sufficient_support",
            "missing_signals": ["广告流量", "售价与促销", "库存与缺货", "站点活动"],
        },
        "evidence": evidence,
        "demo_notice": "当前为 Stage 5 确定性数据演示轨迹，真实 LangGraph Agent 将在 Stage 6 接入。",
    }


def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "dataset_root": str(DATASET_ROOT),
        "products": len(list_products()),
        "anomalies": len(list_anomalies()),
    }
