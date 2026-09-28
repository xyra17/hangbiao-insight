from __future__ import annotations

from datetime import date
from typing import Any

from ..engines.metric_engine import Granularity, MetricEngine, canonical_metric
from ..repositories.metric_repository import MetricRepository


class MetricService:
    def __init__(self, repository: MetricRepository | None = None, engine: MetricEngine | None = None):
        self.repository = repository or MetricRepository()
        self.engine = engine or MetricEngine()

    def metric_query(
        self,
        product_id: str,
        metric_name: str,
        start_date: date,
        end_date: date,
        granularity: Granularity = "month",
    ) -> dict[str, Any]:
        metric = canonical_metric(metric_name)
        rows = self.repository.query(product_id, start_date, end_date)
        return {
            "product_id": product_id,
            "metric_name": metric,
            "granularity": granularity,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "points": self.engine.aggregate(rows, metric, granularity),
            "data_source": "synthetic",
            "metric_version": "business-metric-v1.0",
        }

    def metric_trend(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.metric_query(*args, **kwargs)

    def metric_aggregate(
        self,
        product_id: str,
        metric_name: str,
        start_date: date,
        end_date: date,
        granularity: Granularity = "month",
    ) -> dict[str, Any]:
        metric = canonical_metric(metric_name)
        rows = self.repository.query(product_id, start_date, end_date)
        value = self.engine.summarize(rows, metric, granularity)
        return {
            "product_id": product_id,
            "metric_name": metric,
            "granularity": granularity,
            "value": round(value, 10) if value is not None else None,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "observation_days": len(rows),
            "data_source": "synthetic",
            "metric_version": "business-metric-v1.0",
        }

    def metric_compare(
        self,
        product_id: str,
        metric_name: str,
        current_start: date,
        current_end: date,
        baseline_start: date,
        baseline_end: date,
        granularity: Granularity = "month",
    ) -> dict[str, Any]:
        metric = canonical_metric(metric_name)
        current = self.metric_aggregate(product_id, metric, current_start, current_end, granularity)
        baseline = self.metric_aggregate(product_id, metric, baseline_start, baseline_end, granularity)
        current_value, baseline_value = current["value"], baseline["value"]
        if current_value is None or baseline_value is None:
            absolute_delta = relative_change = None
        else:
            absolute_delta = current_value - baseline_value
            relative_change = absolute_delta / abs(baseline_value) if baseline_value else None
        return {
            "product_id": product_id,
            "metric_name": metric,
            "granularity": granularity,
            "current_value": current_value,
            "baseline_value": baseline_value,
            "absolute_delta": round(absolute_delta, 10) if absolute_delta is not None else None,
            "relative_change": round(relative_change, 10) if relative_change is not None else None,
            "current_start": current_start.isoformat(),
            "current_end": current_end.isoformat(),
            "baseline_start": baseline_start.isoformat(),
            "baseline_end": baseline_end.isoformat(),
            "data_source": "synthetic",
            "metric_version": "business-metric-v1.0",
        }

