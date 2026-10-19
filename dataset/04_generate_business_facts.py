#!/usr/bin/env python3
"""Generate the reproducible business-fact augmentation package.

The script only writes ``dataset/data/04_经营事实增强`` (or ``--output-dir``).
It never edits the standardized review files, the golden review files, or the
existing synthetic business-metric files.  Generated rows deliberately carry
provenance columns; ``prepare_import.py`` removes those columns when an
ingestion-ready file is needed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = BASE_DIR / "data" / "04_经营事实增强"
METRIC_FILE = BASE_DIR / "data" / "02_构造经营指标" / "经营指标日宽表_v1.csv"
REVIEW_FILE = BASE_DIR / "data" / "01_标准化评论数据" / "评论数据集_v1.csv"
GOLD_LABEL_FILE = BASE_DIR / "data" / "03_黄金评论集" / "黄金评论标签表_v1_0.csv"

GENERATOR_VERSION = "business-facts-augmentation-v1.0"
DEFAULT_SEED = 20260929
METADATA_FIELDS = [
    "provenance_class",
    "is_real",
    "is_derived",
    "is_synthetic",
    "source_reference",
    "source_row_id",
    "generation_version",
    "scenario_id",
    "boundary_case",
    "currency_case",
    "synthetic_from_review",
]

CONTRACTS = {
    "sales_traffic_daily_v1.csv": {
        "kind": "sales_traffic",
        "grain": "asin_day",
        "core_fields": [
            "child_asin",
            "seller_sku",
            "fulfillment",
            "business_date",
            "ordered_units",
            "order_count",
            "sessions",
            "buybox_percent",
            "sales_amount",
            "currency",
        ],
        "intended_use": "销售、流量、转化和金额的可重复本地验收；全部行来自已有合成经营指标并补充派生字段。",
    },
    "ratings_daily_v1.csv": {
        "kind": "ratings",
        "grain": "asin_day",
        "core_fields": ["child_asin", "seller_sku", "fulfillment", "business_date", "rating", "rating_count"],
        "intended_use": "页面星级与累计评分数验收；星级由现有真实评论聚合得到，不表示实时 Amazon 页面快照。",
    },
    "inventory_daily_v1.csv": {
        "kind": "inventory",
        "grain": "asin_day",
        "core_fields": [
            "child_asin",
            "seller_sku",
            "fulfillment",
            "business_date",
            "available",
            "reserved",
            "inbound",
            "unsellable",
            "total",
            "listing_status",
        ],
        "intended_use": "库存、预留、在途、不可售和 listing 状态的契约与缺货边界验收。",
    },
    "operations_v1.csv": {
        "kind": "operations",
        "grain": "asin_record",
        "core_fields": [
            "child_asin",
            "seller_sku",
            "fulfillment",
            "business_date",
            "record_id",
            "event_type",
            "description",
            "ends_on",
            "price",
            "currency",
            "mechanism",
        ],
        "intended_use": "价格、促销、库存运营事件的导入和版本刷新验收。",
    },
    "orders_v1.csv": {
        "kind": "orders",
        "grain": "product_record",
        "core_fields": [
            "child_asin",
            "seller_sku",
            "fulfillment",
            "business_date",
            "record_id",
            "order_id",
            "quantity",
            "shipped_date",
            "shipped_at",
            "shipped_quantity",
            "original_line_id",
            "is_exchange",
            "status",
        ],
        "intended_use": "订单、拆分发货、修订、取消、迟到发货和撤销候选的生命周期验收。",
    },
    "refunds_v1.csv": {
        "kind": "refunds",
        "grain": "product_record",
        "core_fields": [
            "child_asin",
            "seller_sku",
            "fulfillment",
            "business_date",
            "record_id",
            "order_line_id",
            "transaction_chain_id",
            "amount",
            "currency",
            "quantity",
            "occurred_at",
        ],
        "intended_use": "部分/全额退款、迟到退款、冲正、多币种和未知币种的成熟队列验收。",
    },
    "returns_v1.csv": {
        "kind": "returns",
        "grain": "product_record",
        "core_fields": [
            "child_asin",
            "seller_sku",
            "fulfillment",
            "business_date",
            "record_id",
            "order_line_id",
            "quantity",
            "reason",
            "status",
            "requested_date",
            "received_date",
            "requested_at",
            "received_at",
        ],
        "intended_use": "requested→received→completed/cancelled/rejected、跨月及未知状态归一化验收。",
    },
    "support_cases_v1.csv": {
        "kind": "support",
        "grain": "asin_record",
        "core_fields": [
            "child_asin",
            "seller_sku",
            "fulfillment",
            "business_date",
            "record_id",
            "order_line_id",
            "text",
            "status",
        ],
        "intended_use": "从黄金评论主题生成的客服工单检索和关联验收；不是 Amazon 或真实客服系统导出。",
    },
}

PRODUCT_CONFIG = {
    "B07P9S6R9Y": {"price": Decimal("19.99"), "initial_stock": 1400},
    "B0BYJ8ZVWH": {"price": Decimal("15.99"), "initial_stock": 900},
    "B07H2V5YLH": {"price": Decimal("9.99"), "initial_stock": 1100},
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def stable_int(seed: int, key: str, modulo: int = 10_000) -> int:
    digest = hashlib.sha256(f"{seed}|{key}".encode()).hexdigest()
    return int(digest[:12], 16) % modulo


def money(value: Decimal | float) -> str:
    return format(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def iso_utc(day: date, hour: int = 12, minute: int = 0) -> str:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def as_bool(value: bool) -> str:
    return "true" if value else "false"


def mark(
    row: dict[str, object],
    *,
    provenance_class: str,
    is_real: bool,
    is_derived: bool,
    is_synthetic: bool,
    source_reference: str,
    source_row_id: str,
    scenario_id: str = "",
    boundary_case: str = "",
    currency_case: str = "",
    synthetic_from_review: str = "",
) -> dict[str, object]:
    row.update(
        {
            "provenance_class": provenance_class,
            "is_real": as_bool(is_real),
            "is_derived": as_bool(is_derived),
            "is_synthetic": as_bool(is_synthetic),
            "source_reference": source_reference,
            "source_row_id": source_row_id,
            "generation_version": GENERATOR_VERSION,
            "scenario_id": scenario_id or "",
            "boundary_case": boundary_case or "",
            "currency_case": currency_case or "",
            "synthetic_from_review": synthetic_from_review or "",
        }
    )
    return row


def parse_day(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def product_days(metric_rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in metric_rows:
        asin = row.get("product_id", "").strip()
        if asin in PRODUCT_CONFIG:
            grouped[asin].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: parse_day(row["date"]))
    return dict(grouped)


def build_reviews() -> tuple[dict[str, list[dict[str, str]]], dict[str, dict[str, str]]]:
    by_product: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_id: dict[str, dict[str, str]] = {}
    for row in read_csv(REVIEW_FILE):
        if row.get("sku") in PRODUCT_CONFIG:
            row = dict(row)
            row["review_day"] = parse_day(row.get("created_at", ""))
            by_product[row["sku"]].append(row)
            by_id[row["review_id"]] = row
    for rows in by_product.values():
        rows.sort(key=lambda row: (row["review_day"], row["review_id"]))
    return dict(by_product), by_id


def metadata_columns(rows: list[dict[str, object]], core_fields: list[str]) -> list[str]:
    extras: list[str] = []
    for row in rows:
        for key in row:
            if key not in core_fields + METADATA_FIELDS and key not in extras:
                extras.append(key)
    return core_fields + METADATA_FIELDS + extras


def write_rows(path: Path, rows: list[dict[str, object]], core_fields: list[str]) -> list[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = metadata_columns(rows, core_fields)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: "" if row.get(field) is None else row.get(field, "") for field in fields})
    return fields


def generate_sales_and_daily(
    metrics: dict[str, list[dict[str, str]]],
    reviews: dict[str, list[dict[str, str]]],
    seed: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], dict[str, list[dict[str, object]]]]:
    sales: list[dict[str, object]] = []
    ratings: list[dict[str, object]] = []
    inventory: list[dict[str, object]] = []
    daily_by_product: dict[str, list[dict[str, object]]] = {}

    for asin, rows in sorted(metrics.items()):
        review_rows = reviews.get(asin, [])
        review_index = 0
        cumulative_count = 0
        cumulative_rating = Decimal(0)
        cumulative_units = 0
        cumulative_inbound = 0
        product_daily: list[dict[str, object]] = []
        initial_stock = PRODUCT_CONFIG[asin]["initial_stock"]
        for index, metric in enumerate(rows):
            day = parse_day(metric["date"])
            order_count = int(float(metric.get("order_count") or 0))
            scenario_id = metric.get("scenario_id") or ""
            scenario_type = metric.get("scenario_type") or "baseline"
            units_factor = Decimal("1.05") + Decimal(stable_int(seed, f"units|{asin}|{day}", 7)) / Decimal(100)
            ordered_units = max(order_count, int((Decimal(order_count) * units_factor).to_integral_value(rounding=ROUND_HALF_UP)))
            sessions = max(order_count, order_count * 4 + stable_int(seed, f"sessions|{asin}|{day}", 31))
            buybox = Decimal(88) + Decimal(stable_int(seed, f"buybox|{asin}|{day}", 900)) / Decimal(100) - Decimal("4.5")
            price = PRODUCT_CONFIG[asin]["price"]
            if scenario_type in {"positive_recovery", "negative_reversal"} and index % 2 == 0:
                price *= Decimal("0.92")
            if scenario_type == "explainable_negative":
                price *= Decimal("0.95")
            sales_row = mark(
                {
                    "child_asin": asin,
                    "seller_sku": "",
                    "fulfillment": "",
                    "business_date": day.isoformat(),
                    "ordered_units": ordered_units,
                    "order_count": order_count,
                    "sessions": sessions,
                    "buybox_percent": money(buybox),
                    "sales_amount": money(Decimal(ordered_units) * price),
                    "currency": "USD",
                },
                provenance_class="derived_from_synthetic_metrics",
                is_real=False,
                is_derived=True,
                is_synthetic=True,
                source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
                source_row_id=f"metric:{asin}:{day.isoformat()}",
                scenario_id=scenario_id,
            )
            sales.append(sales_row)
            product_daily.append(sales_row)

            while review_index < len(review_rows) and review_rows[review_index]["review_day"] <= day:
                review = review_rows[review_index]
                cumulative_count += 1
                cumulative_rating += Decimal(str(review.get("rating") or "0"))
                review_index += 1
            rating_value = (cumulative_rating / cumulative_count) if cumulative_count else Decimal(0)
            ratings.append(
                mark(
                    {
                        "child_asin": asin,
                        "seller_sku": "",
                        "fulfillment": "",
                        "business_date": day.isoformat(),
                        "rating": money(rating_value),
                        "rating_count": cumulative_count,
                    },
                    provenance_class="derived_from_real_reviews",
                    is_real=False,
                    is_derived=True,
                    is_synthetic=False,
                    source_reference="dataset/data/01_标准化评论数据/评论数据集_v1.csv",
                    source_row_id=f"review_aggregate:{asin}:{day.isoformat()}",
                    scenario_id=scenario_id,
                )
            )

            reserved = max(0, round(ordered_units * 0.06))
            unsellable = 2 if index % 47 == 0 else 0
            inbound = max(0, round(ordered_units * (Decimal("1.08") + Decimal(stable_int(seed, f"inbound|{asin}|{day}", 6)) / Decimal(100))))
            if scenario_type in {"explainable_negative", "negative_reversal"} and index % 3 == 0:
                inbound = max(0, inbound // 2)
            cumulative_units += ordered_units
            cumulative_inbound += inbound
            available = max(0, initial_stock + cumulative_inbound - cumulative_units - reserved - unsellable)
            listing_status = "active" if available > 0 else "suppressed"
            inventory.append(
                mark(
                    {
                        "child_asin": asin,
                        "seller_sku": "",
                        "fulfillment": "",
                        "business_date": day.isoformat(),
                        "available": available,
                        "reserved": reserved,
                        "inbound": inbound,
                        "unsellable": unsellable,
                        "total": available + reserved + inbound + unsellable,
                        "listing_status": listing_status,
                    },
                    provenance_class="derived_from_synthetic_metrics",
                    is_real=False,
                    is_derived=True,
                    is_synthetic=True,
                    source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
                    source_row_id=f"metric:{asin}:{day.isoformat()}",
                    scenario_id=scenario_id,
                    boundary_case="stockout" if listing_status == "suppressed" else "",
                )
            )
        daily_by_product[asin] = product_daily
    return sales, ratings, inventory, daily_by_product


def generate_operations(metrics: dict[str, list[dict[str, str]]], seed: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for asin, metric_rows in sorted(metrics.items()):
        days = [parse_day(row["date"]) for row in metric_rows]
        if not days:
            continue
        base_price = PRODUCT_CONFIG[asin]["price"]
        rows.append(
            mark(
                {
                    "child_asin": asin,
                    "seller_sku": "",
                    "fulfillment": "",
                    "business_date": days[0].isoformat(),
                    "record_id": f"OP-{asin}-{days[0].isoformat()}-BASE",
                    "event_type": "price",
                    "description": "合成基线价格事件，用于导入和版本追踪。",
                    "ends_on": "",
                    "price": money(base_price),
                    "currency": "USD",
                    "mechanism": "base_price",
                },
                provenance_class="derived_from_synthetic_metrics",
                is_real=False,
                is_derived=True,
                is_synthetic=True,
                source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
                source_row_id=f"metric:{asin}:{days[0].isoformat()}",
            )
        )
        seen_months: set[tuple[int, int]] = set()
        for index, metric in enumerate(metric_rows):
            day = parse_day(metric["date"])
            scenario_id = metric.get("scenario_id") or ""
            scenario_type = metric.get("scenario_type") or "baseline"
            month_key = (day.year, day.month)
            if day.day == 1 or (month_key not in seen_months and index == 0):
                seen_months.add(month_key)
                promo_price = base_price * Decimal("0.90")
                rows.append(
                    mark(
                        {
                            "child_asin": asin,
                            "seller_sku": "",
                            "fulfillment": "",
                            "business_date": day.isoformat(),
                            "record_id": f"OP-{asin}-{day.isoformat()}-PROMO",
                            "event_type": "promotion",
                            "description": "合成月度优惠活动，用于价格促销事件链路验收。",
                            "ends_on": (day + timedelta(days=6)).isoformat(),
                            "price": money(promo_price),
                            "currency": "USD",
                            "mechanism": "coupon_10pct",
                        },
                        provenance_class="derived_from_synthetic_metrics",
                        is_real=False,
                        is_derived=True,
                        is_synthetic=True,
                        source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
                        source_row_id=f"metric:{asin}:{day.isoformat()}",
                        scenario_id=scenario_id,
                    )
                )
            if scenario_type != "baseline":
                rows.append(
                    mark(
                        {
                            "child_asin": asin,
                            "seller_sku": "",
                            "fulfillment": "",
                            "business_date": day.isoformat(),
                            "record_id": f"OP-{asin}-{day.isoformat()}-SCENARIO",
                            "event_type": "inventory" if "negative" in scenario_type else "promotion",
                            "description": f"合成场景事件：{scenario_type}。仅用于关联验收，不表示真实运营动作。",
                            "ends_on": (day + timedelta(days=2)).isoformat(),
                            "price": money(base_price * Decimal("0.95")),
                            "currency": "EUR" if index % 2 else "USD",
                            "mechanism": "scenario_injection",
                        },
                        provenance_class="derived_from_synthetic_metrics",
                        is_real=False,
                        is_derived=True,
                        is_synthetic=True,
                        source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
                        source_row_id=f"metric:{asin}:{day.isoformat()}",
                        scenario_id=scenario_id,
                        boundary_case="scenario_event",
                        currency_case="multi_currency" if index % 2 else "",
                    )
                )
    return rows


def build_orders(metrics: dict[str, list[dict[str, str]]], seed: int) -> tuple[list[dict[str, object]], dict[str, str], dict[str, list[date]]]:
    rows: list[dict[str, object]] = []
    line_by_product_day: dict[str, str] = {}
    days_by_product: dict[str, list[date]] = {}
    for asin, metric_rows in sorted(metrics.items()):
        product_days_list: list[date] = []
        for index, metric in enumerate(metric_rows):
            day = parse_day(metric["date"])
            product_days_list.append(day)
            record_id = f"OL-{asin}-{day.isoformat()}"
            order_id = f"ORD-{asin}-{day.isoformat()}"
            quantity = 1 + stable_int(seed, f"quantity|{asin}|{day}", 3)
            status = "shipped"
            shipped_date: date | None = day + timedelta(days=1)
            boundary_case = ""
            if index % 53 == 0:
                status, shipped_date, boundary_case = "pending", None, "requested_pending"
            elif index % 97 == 0:
                status, shipped_date, boundary_case = "cancelled", None, "cancelled"
            elif index % 181 == 0:
                shipped_date, boundary_case = day + timedelta(days=12), "late_arrival"
            elif index % 211 == 0:
                boundary_case = "exchange"
            row = mark(
                {
                    "child_asin": asin,
                    "seller_sku": f"{asin}-FBA",
                    "fulfillment": "FBA",
                    "business_date": day.isoformat(),
                    "record_id": record_id,
                    "order_id": order_id,
                    "quantity": quantity,
                    "shipped_date": shipped_date.isoformat() if shipped_date else "",
                    "shipped_at": iso_utc(shipped_date, 15) if shipped_date else "",
                    "shipped_quantity": quantity if shipped_date else "",
                    "original_line_id": "",
                    "is_exchange": as_bool(boundary_case == "exchange"),
                    "status": status,
                },
                provenance_class="synthetic",
                is_real=False,
                is_derived=False,
                is_synthetic=True,
                source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
                source_row_id=f"metric:{asin}:{day.isoformat()}",
                scenario_id=metric.get("scenario_id") or "",
                boundary_case=boundary_case,
            )
            rows.append(row)
            line_by_product_day[f"{asin}|{day.isoformat()}"] = record_id
        days_by_product[asin] = product_days_list

        first_day = product_days_list[0]
        original_line = line_by_product_day[f"{asin}|{first_day.isoformat()}"]
        rows.append(
            mark(
                {
                    "child_asin": asin,
                    "seller_sku": f"{asin}-FBA",
                    "fulfillment": "FBA",
                    "business_date": first_day.isoformat(),
                    "record_id": f"{original_line}-REV1",
                    "order_id": f"ORD-{asin}-{first_day.isoformat()}",
                    "quantity": 2,
                    "shipped_date": (first_day + timedelta(days=2)).isoformat(),
                    "shipped_at": iso_utc(first_day + timedelta(days=2), 16),
                    "shipped_quantity": 2,
                    "original_line_id": original_line,
                    "is_exchange": "false",
                    "status": "revised",
                },
                provenance_class="synthetic",
                is_real=False,
                is_derived=False,
                is_synthetic=True,
                source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
                source_row_id=f"metric:{asin}:{first_day.isoformat()}",
                boundary_case="revision",
            )
        )
        retract_day = product_days_list[min(3, len(product_days_list) - 1)]
        rows.append(
            mark(
                {
                    "child_asin": asin,
                    "seller_sku": f"{asin}-FBA",
                    "fulfillment": "FBA",
                    "business_date": retract_day.isoformat(),
                    "record_id": f"OL-{asin}-{retract_day.isoformat()}-RETRACT",
                    "order_id": f"ORD-{asin}-{retract_day.isoformat()}-RETRACT",
                    "quantity": 1,
                    "shipped_date": "",
                    "shipped_at": "",
                    "shipped_quantity": "",
                    "original_line_id": "",
                    "is_exchange": "false",
                    "status": "cancelled",
                },
                provenance_class="synthetic",
                is_real=False,
                is_derived=False,
                is_synthetic=True,
                source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
                source_row_id=f"metric:{asin}:{retract_day.isoformat()}",
                boundary_case="retraction_candidate",
            )
        )
    return rows, line_by_product_day, days_by_product


def build_refunds(
    metrics: dict[str, list[dict[str, str]]], line_by_product_day: dict[str, str], seed: int
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for asin, metric_rows in sorted(metrics.items()):
        days = [parse_day(row["date"]) for row in metric_rows]
        for index, metric in enumerate(metric_rows):
            day = days[index]
            if int(float(metric.get("refund_count") or 0)) <= 0:
                continue
            line_id = line_by_product_day[f"{asin}|{day.isoformat()}"]
            price = PRODUCT_CONFIG[asin]["price"]
            partial = index % 5 == 0
            amount = price * (Decimal("0.45") if partial else Decimal("1.00"))
            currency = "USD"
            currency_case = ""
            if index % 111 == 0:
                currency, currency_case = "EUR", "multi_currency"
            elif index % 157 == 0:
                currency, currency_case = "ZZZ", "unknown_currency"
            rows.append(
                mark(
                    {
                        "child_asin": asin,
                        "seller_sku": f"{asin}-FBA",
                        "fulfillment": "FBA",
                        "business_date": day.isoformat(),
                        "record_id": f"RF-{asin}-{day.isoformat()}",
                        "order_line_id": line_id,
                        "transaction_chain_id": f"CHAIN-{asin}-{day.isoformat()}",
                        "amount": money(amount),
                        "currency": currency,
                        "quantity": 1,
                        "occurred_at": iso_utc(day, 18),
                        "refund_type": "partial" if partial else "full",
                    },
                    provenance_class="synthetic",
                    is_real=False,
                    is_derived=False,
                    is_synthetic=True,
                    source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
                    source_row_id=f"metric:{asin}:{day.isoformat()}",
                    scenario_id=metric.get("scenario_id") or "",
                    boundary_case="partial_refund" if partial else "full_refund",
                    currency_case=currency_case,
                )
            )

            if index % 60 == 0:
                reversal_day = day + timedelta(days=1)
                rows.append(
                    mark(
                        {
                            "child_asin": asin,
                            "seller_sku": f"{asin}-FBA",
                            "fulfillment": "FBA",
                            "business_date": reversal_day.isoformat(),
                            "record_id": f"RF-{asin}-{day.isoformat()}-REVERSAL",
                            "order_line_id": line_id,
                            "transaction_chain_id": f"CHAIN-{asin}-{day.isoformat()}",
                            "amount": money(-amount),
                            "currency": currency,
                            "quantity": 1,
                            "occurred_at": iso_utc(reversal_day, 19),
                            "refund_type": "reversal",
                        },
                        provenance_class="synthetic",
                        is_real=False,
                        is_derived=False,
                        is_synthetic=True,
                        source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
                        source_row_id=f"metric:{asin}:{day.isoformat()}",
                        scenario_id=metric.get("scenario_id") or "",
                        boundary_case="reversal",
                        currency_case=currency_case,
                    )
                )
            if index % 73 == 0 and index + 30 < len(days):
                late_day = days[index + 30]
                rows.append(
                    mark(
                        {
                            "child_asin": asin,
                            "seller_sku": f"{asin}-FBA",
                            "fulfillment": "FBA",
                            "business_date": late_day.isoformat(),
                            "record_id": f"RF-{asin}-{day.isoformat()}-LATE",
                            "order_line_id": line_id,
                            "transaction_chain_id": f"CHAIN-{asin}-{day.isoformat()}-LATE",
                            "amount": money(price * Decimal("0.60")),
                            "currency": "USD",
                            "quantity": 1,
                            "occurred_at": iso_utc(late_day, 20),
                            "refund_type": "late",
                        },
                        provenance_class="synthetic",
                        is_real=False,
                        is_derived=False,
                        is_synthetic=True,
                        source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
                        source_row_id=f"metric:{asin}:{day.isoformat()}",
                        scenario_id=metric.get("scenario_id") or "",
                        boundary_case="late_arrival",
                    )
                )
    return rows


def build_returns(
    metrics: dict[str, list[dict[str, str]]], line_by_product_day: dict[str, str], seed: int
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    statuses = ["requested", "received", "completed", "cancelled", "rejected", "unknown"]
    reasons = {
        "requested": "customer_requested",
        "received": "defective_on_arrival",
        "completed": "not_as_described",
        "cancelled": "customer_cancelled",
        "rejected": "outside_policy",
        "unknown": "unmapped_source_code",
    }
    for asin, metric_rows in sorted(metrics.items()):
        days = [parse_day(row["date"]) for row in metric_rows]
        for index in range(7, len(days), 19):
            requested_day = days[index]
            received_day = requested_day + timedelta(days=5)
            cross_month = requested_day.month != received_day.month
            if received_day > days[-1]:
                received_day = None
            status = statuses[(index // 19) % len(statuses)]
            if status in {"received", "completed"} and received_day is None:
                status = "requested"
            status_value = "source_pending_vendor_v2" if status == "unknown" else status
            status_has_received = status in {"received", "completed"}
            business_day = received_day if status in {"received", "completed"} and received_day else requested_day
            line_id = line_by_product_day[f"{asin}|{requested_day.isoformat()}"]
            rows.append(
                mark(
                    {
                        "child_asin": asin,
                        "seller_sku": f"{asin}-FBA",
                        "fulfillment": "FBA",
                        "business_date": business_day.isoformat(),
                        "record_id": f"RT-{asin}-{requested_day.isoformat()}",
                        "order_line_id": line_id,
                        "quantity": 1,
                        "reason": reasons[status],
                        "status": status_value,
                        "requested_date": requested_day.isoformat(),
                        "received_date": received_day.isoformat() if received_day and status_has_received else "",
                        "requested_at": iso_utc(requested_day, 9),
                        "received_at": iso_utc(received_day, 17) if received_day and status_has_received else "",
                    },
                    provenance_class="synthetic",
                    is_real=False,
                    is_derived=False,
                    is_synthetic=True,
                    source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
                    source_row_id=f"metric:{asin}:{requested_day.isoformat()}",
                    boundary_case=("unknown_status_code" if status == "unknown" else "cross_month_return" if cross_month else "lifecycle_status"),
                    currency_case="",
                )
            )
    return rows


def category_for_topic(topic: str, secondary: str) -> str:
    if topic == "safety_risk":
        return "safety_issue"
    if topic == "compatibility_fit":
        return "compatibility_issue"
    if topic in {"function_performance", "convenience_value"}:
        return "performance_issue"
    if topic in {"reliability_durability", "product_quality"}:
        return "product_failure"
    if topic in {"usability_installation", "other_unknown"}:
        if "shipping" in secondary or "delivery" in secondary:
            return "logistics_issue"
        return "product_failure"
    return "logistics_issue"


def synthetic_support_text(category: str) -> str:
    templates = {
        "product_failure": "A synthetic case reports that the product stopped working during ordinary use.",
        "performance_issue": "A synthetic case reports that product performance is below the expected level.",
        "compatibility_issue": "A synthetic case asks for help checking compatibility with a device.",
        "safety_issue": "A synthetic case reports a possible overheating concern and requests safety review.",
        "logistics_issue": "A synthetic case asks for an update after a delayed delivery.",
    }
    return f"[SYNTHETIC SUPPORT CASE] {templates.get(category, 'A synthetic case requests product assistance.')} This generated fixture is not a real customer statement or support ticket."


def build_support(
    gold_labels: list[dict[str, str]],
    review_by_id: dict[str, dict[str, str]],
    line_by_product_day: dict[str, str],
    days_by_product: dict[str, list[date]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for label in gold_labels:
        review_id = label.get("review_id", "")
        review = review_by_id.get(review_id, {})
        asin = label.get("product_id", "")
        if asin not in days_by_product:
            continue
        review_day = parse_day(review.get("created_at", "")) if review.get("created_at") else days_by_product[asin][0]
        closest_day = min(days_by_product[asin], key=lambda candidate: abs((candidate - review_day).days))
        category = category_for_topic(label.get("primary_topic", ""), label.get("secondary_topic", ""))
        text = synthetic_support_text(category)
        polarity = label.get("polarity", "").lower()
        status = "open" if polarity == "negative" else "resolved" if polarity == "positive" else "pending"
        rows.append(
            mark(
                {
                    "child_asin": asin,
                    "seller_sku": "",
                    "fulfillment": "",
                    "business_date": closest_day.isoformat(),
                    "record_id": f"SC-{label.get('claim_id') or review_id}",
                    "order_line_id": line_by_product_day.get(f"{asin}|{closest_day.isoformat()}", ""),
                    "text": text,
                    "status": status,
                    "case_category": category,
                    "primary_topic": label.get("primary_topic", ""),
                    "secondary_topic": label.get("secondary_topic", ""),
                    "polarity": label.get("polarity", ""),
                },
                provenance_class="synthetic_from_golden_review_theme",
                is_real=False,
                is_derived=False,
                is_synthetic=True,
                source_reference="dataset/data/03_黄金评论集/黄金评论标签表_v1_0.csv",
                source_row_id=label.get("claim_id") or review_id,
                boundary_case="synthetic_from_review",
                synthetic_from_review=review_id,
            )
        )
    # Guarantee a logistics example even when the frozen taxonomy has no logistics topic.
    if rows and not any(row.get("case_category") == "logistics_issue" for row in rows):
        source = rows[0]
        asin = str(source["child_asin"])
        review_id = str(source.get("synthetic_from_review") or "")
        rows.append(
            mark(
                {
                    "child_asin": asin,
                    "seller_sku": "",
                    "fulfillment": "",
                    "business_date": source["business_date"],
                    "record_id": f"SC-LOGISTICS-{review_id}",
                    "order_line_id": source.get("order_line_id", ""),
                    "text": synthetic_support_text("logistics_issue"),
                    "status": "open",
                    "case_category": "logistics_issue",
                    "primary_topic": "derived_logistics_fixture",
                    "secondary_topic": "delivery_delay",
                    "polarity": "negative",
                },
                provenance_class="synthetic_from_golden_review_theme",
                is_real=False,
                is_derived=False,
                is_synthetic=True,
                source_reference="dataset/data/03_黄金评论集/黄金评论标签表_v1_0.csv",
                source_row_id=review_id,
                boundary_case="synthetic_from_review",
                synthetic_from_review=review_id,
            )
        )
    return rows


def write_boundary_fixtures(output_dir: Path, contracts: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    boundary_dir = output_dir / "boundary_cases"
    boundary_dir.mkdir(parents=True, exist_ok=True)
    invalid_rows = [
        {
            "kind": "orders",
            "file": "orders_invalid_v1.csv",
            "boundary_case": "invalid_encoding",
            "expected_error": "child_asin 须为 10 位大写字母或数字",
            "notes": "故意的非法长度/格式 ASIN；不进入主导入文件。",
        },
        {
            "kind": "orders",
            "file": "orders_duplicate_v1.csv",
            "boundary_case": "duplicate",
            "expected_error": "同一文件内业务键重复",
            "notes": "同一 record_id 重复两行；用于导入解析拒绝。",
        },
        {
            "kind": "refunds",
            "file": "refunds_invalid_v1.csv",
            "boundary_case": "invalid_currency",
            "expected_error": "currency 须为三位大写代码",
            "notes": "两位币种；不进入主导入文件。",
        },
        {
            "kind": "returns",
            "file": "returns_invalid_v1.csv",
            "boundary_case": "invalid_date_order",
            "expected_error": "退货收到日期不能早于申请日期",
            "notes": "received_date 早于 requested_date；不进入主导入文件。",
        },
    ]
    for item in invalid_rows:
        filename = str(item["file"])
        kind = str(item["kind"])
        contract = next(value for name, value in contracts.items() if value["kind"] == kind)
        fields = list(contract["core_fields"])
        if filename.startswith("orders"):
            row = {field: "" for field in fields}
            row.update({"child_asin": "bad-asn1", "seller_sku": "B07P9S6R9Y-FBA", "fulfillment": "FBA", "business_date": "2023-01-01", "record_id": "INVALID-1", "order_id": "INVALID-1", "quantity": "1", "status": "shipped"})
            if item["boundary_case"] == "duplicate":
                row["child_asin"] = "B07P9S6R9Y"
                row["business_date"] = "2023-01-01"
                row["record_id"] = "DUP-1"
                row["order_id"] = "DUP-1"
                rows_to_write = [row, dict(row)]
            else:
                rows_to_write = [row]
        elif filename.startswith("refunds"):
            row = {field: "" for field in fields}
            row.update({"child_asin": "B07P9S6R9Y", "seller_sku": "B07P9S6R9Y-FBA", "fulfillment": "FBA", "business_date": "2023-01-01", "record_id": "INVALID-REFUND", "order_line_id": "OL-B07P9S6R9Y-2023-01-01", "transaction_chain_id": "CHAIN-INVALID", "amount": "1.00", "currency": "US", "quantity": "1", "occurred_at": "2023-01-01T12:00:00Z"})
            rows_to_write = [row]
        else:
            row = {field: "" for field in fields}
            row.update({"child_asin": "B07P9S6R9Y", "seller_sku": "B07P9S6R9Y-FBA", "fulfillment": "FBA", "business_date": "2023-01-03", "record_id": "INVALID-RETURN", "order_line_id": "OL-B07P9S6R9Y-2023-01-01", "quantity": "1", "reason": "invalid_fixture", "status": "received", "requested_date": "2023-01-03", "received_date": "2023-01-01", "requested_at": "2023-01-03T09:00:00Z", "received_at": "2023-01-01T09:00:00Z"})
            rows_to_write = [row]
        rows_to_write = [
            mark(
                row,
                provenance_class="invalid_boundary_fixture",
                is_real=False,
                is_derived=False,
                is_synthetic=True,
                source_reference="dataset/data/04_经营事实增强/boundary_cases/",
                source_row_id=f"{filename}:{index + 1}",
                boundary_case=str(item["boundary_case"]),
            )
            for index, row in enumerate(rows_to_write)
        ]
        write_rows(boundary_dir / filename, rows_to_write, fields)
    empty_fields = list(next(value for name, value in contracts.items() if value["kind"] == "refunds")["core_fields"])
    with (boundary_dir / "refunds_empty_report_v1.csv").open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(empty_fields)
    manifest = {
        "invalid_fixtures": invalid_rows,
        "empty_report": {
            "file": "boundary_cases/refunds_empty_report_v1.csv",
            "kind": "refunds",
            "rows": 0,
            "requires_contract_flag": "empty_report_means_zero=true",
        },
    }
    (boundary_dir / "README.md").write_text(
        "# 边界夹具\n\n这些文件故意包含非法编码、重复键、日期倒置或零行报表。它们不属于可发布主数据；请通过当前导入 API 单独演练错误预览、`allow_partial=false` 和 `empty_report_means_zero=true`。\n",
        encoding="utf-8",
    )
    return manifest


def write_revision_fixtures(output_dir: Path, metrics: dict[str, list[dict[str, str]]]) -> dict[str, object]:
    """Write small, isolated files for publish/revise/retract walkthroughs."""
    revision_dir = output_dir / "lifecycle_revisions"
    revision_dir.mkdir(parents=True, exist_ok=True)
    asin = min(metrics)
    metric_row = metrics[asin][min(8, len(metrics[asin]) - 1)]
    day = parse_day(metric_row["date"])

    sales_core = list(CONTRACTS["sales_traffic_daily_v1.csv"]["core_fields"])
    sales_v1 = mark(
        {
            "child_asin": asin,
            "seller_sku": "",
            "fulfillment": "",
            "business_date": day.isoformat(),
            "ordered_units": 100,
            "order_count": 90,
            "sessions": 750,
            "buybox_percent": "92.00",
            "sales_amount": "1999.00",
            "currency": "USD",
        },
        provenance_class="synthetic_revision_fixture",
        is_real=False,
        is_derived=False,
        is_synthetic=True,
        source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
        source_row_id=f"metric:{asin}:{day}",
        boundary_case="revision_v1",
    )
    sales_v2 = dict(sales_v1)
    sales_v2.update({"sessions": 900, "buybox_percent": "88.00", "boundary_case": "revision_v2"})
    write_rows(revision_dir / "sales_traffic_revision_v1.csv", [sales_v1], sales_core)
    write_rows(revision_dir / "sales_traffic_revision_v2.csv", [sales_v2], sales_core)

    return_core = list(CONTRACTS["returns_v1.csv"]["core_fields"])
    rma_id = f"RT-TRANSITION-{asin}"
    received_day = day + timedelta(days=5)
    return_v1 = mark(
        {
            "child_asin": asin,
            "seller_sku": f"{asin}-FBA",
            "fulfillment": "FBA",
            "business_date": day.isoformat(),
            "record_id": rma_id,
            "order_line_id": f"OL-{asin}-{day.isoformat()}",
            "quantity": 1,
            "reason": "source_state_transition",
            "status": "requested",
            "requested_date": day.isoformat(),
            "received_date": "",
            "requested_at": iso_utc(day, 9),
            "received_at": "",
        },
        provenance_class="synthetic_revision_fixture",
        is_real=False,
        is_derived=False,
        is_synthetic=True,
        source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
        source_row_id=f"metric:{asin}:{day}",
        boundary_case="requested_to_received_to_cancelled_v1",
    )
    return_v2 = dict(return_v1)
    return_v2.update(
        {
            "business_date": received_day.isoformat(),
            "status": "received",
            "received_date": received_day.isoformat(),
            "received_at": iso_utc(received_day, 17),
            "boundary_case": "requested_to_received_to_cancelled_v2",
        }
    )
    return_v3 = dict(return_v2)
    return_v3.update({"status": "cancelled", "boundary_case": "requested_to_received_to_cancelled_v3_source_correction"})
    write_rows(revision_dir / "returns_requested_v1.csv", [return_v1], return_core)
    write_rows(revision_dir / "returns_received_v2.csv", [return_v2], return_core)
    write_rows(revision_dir / "returns_cancelled_v3.csv", [return_v3], return_core)

    order_core = list(CONTRACTS["orders_v1.csv"]["core_fields"])
    retract_row = mark(
        {
            "child_asin": asin,
            "seller_sku": f"{asin}-FBA",
            "fulfillment": "FBA",
            "business_date": day.isoformat(),
            "record_id": f"OL-RETRACTION-DEMO-{asin}",
            "order_id": f"ORD-RETRACTION-DEMO-{asin}",
            "quantity": 1,
            "shipped_date": (day + timedelta(days=1)).isoformat(),
            "shipped_at": iso_utc(day + timedelta(days=1), 14),
            "shipped_quantity": 1,
            "original_line_id": "",
            "is_exchange": "false",
            "status": "shipped",
        },
        provenance_class="synthetic_revision_fixture",
        is_real=False,
        is_derived=False,
        is_synthetic=True,
        source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
        source_row_id=f"metric:{asin}:{day}",
        boundary_case="publish_then_retract",
    )
    write_rows(revision_dir / "order_retraction_candidate.csv", [retract_row], order_core)
    refund_core = list(CONTRACTS["refunds_v1.csv"]["core_fields"])
    late_order_day = parse_day(metrics[asin][min(20, len(metrics[asin]) - 1)]["date"])
    late_event_day = late_order_day + timedelta(days=35)
    late_generated_at = datetime(late_event_day.year, late_event_day.month, late_event_day.day, 12, tzinfo=timezone.utc) + timedelta(days=40)
    late_refund = mark(
        {
            "child_asin": asin,
            "seller_sku": f"{asin}-FBA",
            "fulfillment": "FBA",
            "business_date": late_event_day.isoformat(),
            "record_id": f"RF-LATE-ARRIVAL-{asin}",
            "order_line_id": f"OL-{asin}-{late_order_day.isoformat()}",
            "transaction_chain_id": f"CHAIN-LATE-ARRIVAL-{asin}",
            "amount": money(PRODUCT_CONFIG[asin]["price"] * Decimal("0.35")),
            "currency": "USD",
            "quantity": 1,
            "occurred_at": iso_utc(late_event_day, 18),
            "refund_type": "late_arrival_fixture",
        },
        provenance_class="synthetic_revision_fixture",
        is_real=False,
        is_derived=False,
        is_synthetic=True,
        source_reference="dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
        source_row_id=f"metric:{asin}:{late_order_day}",
        boundary_case="late_arrival",
    )
    write_rows(revision_dir / "refund_late_arrival.csv", [late_refund], refund_core)
    return {
        "same_business_key_files": [
            {"file": "lifecycle_revisions/sales_traffic_revision_v1.csv", "key": [asin, day.isoformat()], "step": "first publish"},
            {"file": "lifecycle_revisions/sales_traffic_revision_v2.csv", "key": [asin, day.isoformat()], "step": "corrected revision"},
            {"file": "lifecycle_revisions/returns_requested_v1.csv", "key": [rma_id], "step": "publish requested"},
            {"file": "lifecycle_revisions/returns_received_v2.csv", "key": [rma_id], "step": "received state revision"},
            {"file": "lifecycle_revisions/returns_cancelled_v3.csv", "key": [rma_id], "step": "source correction after receipt"},
        ],
        "retraction_file": "lifecycle_revisions/order_retraction_candidate.csv",
        "late_arrival_file": "lifecycle_revisions/refund_late_arrival.csv",
        "late_arrival_generated_at": late_generated_at.isoformat().replace("+00:00", "Z"),
        "late_arrival_business_date": late_event_day.isoformat(),
        "retraction_steps": [
            "upload and publish the candidate",
            "call POST /api/v1/imports/{batch_id}/revoke/ with a reason",
            "verify latest-facts hides the row while original raw row and fact remain auditable",
        ],
    }


def source_manifest(generated_at: str) -> list[dict[str, object]]:
    return [
        {
            "path": "dataset/data/01_标准化评论数据/评论数据集_v1.csv",
            "source": "McAuley-Lab/Amazon-Reviews-2023（项目已固定的抽取子集）",
            "provenance": "real",
            "license": "上游仓库含 MIT LICENSE 文件，但该文件是否覆盖 Amazon 评论内容并不明确；本包不复制评论正文，公开/外发前仍须核实数据内容权利。",
            "url": "https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023",
            "used_by": ["ratings_daily", "support_cases themes"],
        },
        {
            "path": "dataset/data/02_构造经营指标/经营指标日宽表_v1.csv",
            "source": "project-generated business-metric-v1.0",
            "provenance": "synthetic",
            "license": "项目生成内容；按本项目仓库许可使用。",
            "used_by": ["sales_traffic_daily", "inventory_daily", "operations", "orders", "refunds", "returns"],
        },
        {
            "path": "dataset/data/03_黄金评论集/黄金评论标签表_v1_0.csv",
            "source": "project-curated golden annotation set",
            "provenance": "derived/curated",
            "license": "项目内部标注产物；仅作为生成输入，不读入运行时答案。",
            "used_by": ["support_cases themes"],
        },
        {"path": "generated_at", "source": generated_at, "provenance": "generation metadata", "license": "n/a", "used_by": []},
    ]


def write_manifest(
    output_dir: Path,
    generated_at: str,
    seed: int,
    files: dict[str, dict[str, object]],
    boundary_manifest: dict[str, object],
    revision_manifest: dict[str, object],
) -> dict[str, object]:
    coverage_start = min(info["coverage_start"] for info in files.values())
    coverage_end = max(info["coverage_end"] for info in files.values())
    manifest = {
        "manifest_version": GENERATOR_VERSION,
        "generated_at": generated_at,
        "generator": "dataset/04_generate_business_facts.py",
        "seed": seed,
        "coverage": {"start": coverage_start, "end": coverage_end, "timezone": "UTC for event timestamps; business_date is marketplace-local fixture date"},
        "inputs": source_manifest(generated_at),
        "fact_contracts": {
            "nine_contracts": ["sales_traffic", "orders", "refunds", "returns", "reviews", "support", "inventory", "ratings", "operations"],
            "reviews": {"status": "unchanged", "path": "dataset/data/01_标准化评论数据/评论数据集_v1.csv", "included_in_generated_files": False},
        },
        "files": files,
        "boundary_cases": boundary_manifest,
        "revision_and_retraction_fixtures": revision_manifest,
        "provenance_rules": {
            "is_real": "仅表示该行是否直接复制真实业务事实；本包生成行均为 false。",
            "is_derived": "由已有评论或合成经营指标按固定规则聚合/补齐。",
            "is_synthetic": "按固定种子构造、重演或从主题模板生成。",
            "provenance_class": "机器可读的来源组合；不得把 synthetic_from_review 当作真实客服来源。",
            "synthetic_from_review": "support_cases 必填，保存其对应黄金评论 review_id。",
        },
        "intended_use": [
            "本地导入契约、版本修订、撤销发布、日期覆盖、退款退货成熟队列、检索关联和指标已知答案验收。",
            "不能代替真实 Amazon Seller Central/财务/客服数据的业务口径、授权、站点时区和生产故障验收。",
        ],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def generate(output_dir: Path = DEFAULT_OUTPUT_DIR, seed: int = DEFAULT_SEED, generated_at: str | None = None) -> dict[str, object]:
    generated_at = generated_at or os.environ.get("BUSINESS_FACTS_GENERATED_AT") or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    output_dir.mkdir(parents=True, exist_ok=True)
    for child in output_dir.iterdir():
        if child.name in {"manifest.json", "validation", "import_ready", "boundary_cases", "lifecycle_revisions", "expected_results.json"} or child.name.endswith("_v1.csv"):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    metric_rows = read_csv(METRIC_FILE)
    metrics = product_days(metric_rows)
    reviews, review_by_id = build_reviews()
    gold_labels = read_csv(GOLD_LABEL_FILE)
    sales, ratings, inventory, _ = generate_sales_and_daily(metrics, reviews, seed)
    operations = generate_operations(metrics, seed)
    orders, line_by_product_day, days_by_product = build_orders(metrics, seed)
    refunds = build_refunds(metrics, line_by_product_day, seed)
    returns = build_returns(metrics, line_by_product_day, seed)
    support = build_support(gold_labels, review_by_id, line_by_product_day, days_by_product)
    row_sets = {
        "sales_traffic_daily_v1.csv": sales,
        "ratings_daily_v1.csv": ratings,
        "inventory_daily_v1.csv": inventory,
        "operations_v1.csv": operations,
        "orders_v1.csv": orders,
        "refunds_v1.csv": refunds,
        "returns_v1.csv": returns,
        "support_cases_v1.csv": support,
    }
    file_manifest: dict[str, dict[str, object]] = {}
    for filename, rows in row_sets.items():
        contract = CONTRACTS[filename]
        fields = write_rows(output_dir / filename, rows, list(contract["core_fields"]))
        dates = [parse_day(str(row["business_date"])) for row in rows if row.get("business_date")]
        asins = sorted({str(row.get("child_asin")) for row in rows if row.get("child_asin")})
        file_manifest[filename] = {
            "version": GENERATOR_VERSION,
            "generated_at": generated_at,
            "fact_kind": contract["kind"],
            "grain": contract["grain"],
            "rows": len(rows),
            "path": f"dataset/data/04_经营事实增强/{filename}",
            "columns": fields,
            "contract_fields": list(contract["core_fields"]),
            "metadata_fields": METADATA_FIELDS,
            "source": (
                ["dataset/data/01_标准化评论数据/评论数据集_v1.csv"]
                if filename == "ratings_daily_v1.csv"
                else ["dataset/data/03_黄金评论集/黄金评论标签表_v1_0.csv"]
                if filename == "support_cases_v1.csv"
                else ["dataset/data/02_构造经营指标/经营指标日宽表_v1.csv"]
            ),
            "provenance": "derived_from_real_reviews" if filename == "ratings_daily_v1.csv" else "synthetic_from_golden_review_theme" if filename == "support_cases_v1.csv" else "derived_or_synthetic_from_business_metric_v1.0",
            "license": (
                "评论聚合衍生结果；上游仓库 MIT 文件不明确授权评论内容，限本仓库内部验收，外发前需核实数据权利。"
                if filename == "ratings_daily_v1.csv"
                else "黄金主题用于生成合成文本和关联；仅供本项目内部测试，不能视作真实客服数据。"
                if filename == "support_cases_v1.csv"
                else "项目生成的合成/派生测试数据，按本项目仓库许可；不含真实 Amazon 经营报表。"
            ),
            "real_fields": ["child_asin", "business_date"] if filename == "ratings_daily_v1.csv" else [],
            "derived_fields": ["rating", "rating_count"] if filename == "ratings_daily_v1.csv" else list(contract["core_fields"]) if filename in {"sales_traffic_daily_v1.csv", "inventory_daily_v1.csv", "operations_v1.csv"} else [],
            "synthetic_fields": ["seller_sku", "fulfillment"] if filename == "ratings_daily_v1.csv" else list(contract["core_fields"]),
            "field_provenance": {
                field: (
                    "derived_from_real_reviews"
                    if filename == "ratings_daily_v1.csv" and field in {"rating", "rating_count"}
                    else "real_reference_from_reviews"
                    if filename == "ratings_daily_v1.csv" and field in {"child_asin", "business_date"}
                    else "metadata_provenance"
                    if field in METADATA_FIELDS
                    else "derived_from_synthetic_metrics"
                    if filename in {"sales_traffic_daily_v1.csv", "inventory_daily_v1.csv", "operations_v1.csv"}
                    else "synthetic"
                )
                for field in fields
            },
            "coverage_start": min(dates).isoformat() if dates else "",
            "coverage_end": max(dates).isoformat() if dates else "",
            "asins": asins,
            "row_provenance_classes": sorted({str(row.get("provenance_class")) for row in rows}),
            "real": all(str(row.get("is_real")) == "true" for row in rows) if rows else False,
            "derived": any(str(row.get("is_derived")) == "true" for row in rows),
            "synthetic": any(str(row.get("is_synthetic")) == "true" for row in rows),
            "intended_use": contract["intended_use"],
        }
    boundary_manifest = write_boundary_fixtures(output_dir, CONTRACTS)
    revision_manifest = write_revision_fixtures(output_dir, metrics)
    manifest = write_manifest(output_dir, generated_at, seed, file_manifest, boundary_manifest, revision_manifest)
    expected = {
        "version": "business-facts-augmentation-expected-v1.0",
        "row_counts": {
            "sales_traffic_daily_v1.csv": 5952,
            "ratings_daily_v1.csv": 5952,
            "inventory_daily_v1.csv": 5952,
            "operations_v1.csv": 414,
            "orders_v1.csv": 5958,
            "refunds_v1.csv": 6082,
            "returns_v1.csv": 313,
            "support_cases_v1.csv": 658,
        },
        "required_boundary_counts_minimum": {
            "late_arrival": 1,
            "partial_refund": 1,
            "full_refund": 1,
            "reversal": 1,
            "cross_month_return": 1,
            "unknown_status_code": 1,
            "unknown_currency": 1,
            "synthetic_from_review": 1,
        },
        "support_categories": ["product_failure", "performance_issue", "compatibility_issue", "safety_issue", "logistics_issue"],
        "fixtures": {"invalid_rows": 4, "empty_refunds_rows": 0, "same_key_revisions": 5, "retraction_candidate_rows": 1},
    }
    (output_dir / "expected_results.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="生成可重复的三批经营事实增强包")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--generated-at", default=None, help="固定 manifest 时间，便于发布包复现")
    args = parser.parse_args()
    manifest = generate(args.output_dir, args.seed, args.generated_at)
    print(json.dumps({"output_dir": str(args.output_dir), "files": {key: value["rows"] for key, value in manifest["files"].items()}, "generated_at": manifest["generated_at"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
