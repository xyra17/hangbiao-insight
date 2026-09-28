from __future__ import annotations

import csv
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DAILY_METRIC_PATH = PROJECT_ROOT / "dataset" / "data" / "02_构造经营指标" / "经营指标日宽表_v1.csv"


@lru_cache(maxsize=1)
def _load_daily_rows() -> tuple[dict[str, Any], ...]:
    with DAILY_METRIC_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            rows.append(
                {
                    **raw,
                    "date": date.fromisoformat(raw["date"]),
                    "order_count": float(raw["order_count"]),
                    "refund_count": float(raw["refund_count"]),
                    "refund_rate": float(raw["refund_rate"]),
                    "is_injected": raw.get("is_injected", "").lower() == "true",
                }
            )
        return tuple(rows)


class MetricRepository:
    """经营指标事实表读取层，不包含业务计算。"""

    def query(
        self,
        product_id: str,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        if start_date > end_date:
            raise ValueError("start_date 不能晚于 end_date")
        return [
            row
            for row in _load_daily_rows()
            if row["product_id"] == product_id and start_date <= row["date"] <= end_date
        ]

    def product_exists(self, product_id: str) -> bool:
        return any(row["product_id"] == product_id for row in _load_daily_rows())

    def available_range(self, product_id: str) -> tuple[date, date] | None:
        dates = [row["date"] for row in _load_daily_rows() if row["product_id"] == product_id]
        return (min(dates), max(dates)) if dates else None

