#!/usr/bin/env python3
"""Convert the augmentation CSVs into exact Django ingestion payloads.

Generated rows include provenance columns so they remain auditable as data
artifacts.  The ingestion endpoint requires unknown columns to be listed in
``ignored_columns``; this helper writes a clean CSV and a sidecar contract
manifest for each fact kind.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR / "data" / "04_经营事实增强"
PROVENANCE_FIELDS = [
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


def parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def convert(data_dir: Path = DEFAULT_DATA_DIR, output_dir: Path | None = None) -> list[dict[str, object]]:
    output_dir = output_dir or data_dir / "import_ready"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    results: list[dict[str, object]] = []
    for filename, info in manifest["files"].items():
        source_path = data_dir / filename
        destination = output_dir / filename
        with source_path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            all_fields = list(reader.fieldnames or [])
            core_fields = list(info["contract_fields"])
            missing = sorted(set(core_fields) - set(all_fields))
            if missing:
                raise ValueError(f"{filename} 缺少契约字段：{', '.join(missing)}")
            import_fields = core_fields + [field for field in PROVENANCE_FIELDS if field in all_fields]
            rows = list(reader)
        with destination.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=import_fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in import_fields})
        sidecar = {
            "source_file": f"dataset/data/04_经营事实增强/{filename}",
            "import_file": f"dataset/data/04_经营事实增强/import_ready/{filename}",
            "provenance": info["provenance"],
            "source_payload": {
                "shop": "<目标测试店铺 UUID>",
                "name": f"增强数据：{info['fact_kind']}（{manifest['manifest_version']}）",
                "kind": info["fact_kind"],
                "is_primary": True,
                "grain": info["grain"],
                "column_mapping": {field: field for field in core_fields},
                "ignored_columns": [field for field in import_fields if field not in core_fields],
                "coverage_basis": "business_date",
                "empty_report_means_zero": False,
                **({"return_event": "received"} if info["fact_kind"] == "returns" else {}),
            },
            "upload_payload": {
                "coverage_start": info["coverage_start"],
                "coverage_end": info["coverage_end"],
                "generated_at": manifest["generated_at"],
                "declared_complete": False,
                "declared_asins": info["asins"],
            },
            "warning": "这些行仍是项目合成/派生数据；发布前须在目标店铺配置真实 ProductMapping、时区和契约。",
        }
        (destination.with_suffix(destination.suffix + ".meta.json")).write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        results.append({"file": str(destination), "rows": len(rows), "kind": info["fact_kind"], "ignored_columns": sidecar["source_payload"]["ignored_columns"]})

    revision_specs = {
        "sales_traffic_revision_v1.csv": ("sales_traffic", "asin_day", manifest["files"]["sales_traffic_daily_v1.csv"]["contract_fields"]),
        "sales_traffic_revision_v2.csv": ("sales_traffic", "asin_day", manifest["files"]["sales_traffic_daily_v1.csv"]["contract_fields"]),
        "returns_requested_v1.csv": ("returns", "product_record", manifest["files"]["returns_v1.csv"]["contract_fields"]),
        "returns_received_v2.csv": ("returns", "product_record", manifest["files"]["returns_v1.csv"]["contract_fields"]),
        "returns_cancelled_v3.csv": ("returns", "product_record", manifest["files"]["returns_v1.csv"]["contract_fields"]),
        "order_retraction_candidate.csv": ("orders", "product_record", manifest["files"]["orders_v1.csv"]["contract_fields"]),
        "refund_late_arrival.csv": ("refunds", "product_record", manifest["files"]["refunds_v1.csv"]["contract_fields"]),
    }
    revision_output = output_dir / "lifecycle_revisions"
    revision_output.mkdir(parents=True, exist_ok=True)
    for filename, (kind, grain, core_fields) in revision_specs.items():
        source_path = data_dir / "lifecycle_revisions" / filename
        with source_path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            all_fields = list(reader.fieldnames or [])
            revision_rows = list(reader)
        import_fields = list(core_fields) + [field for field in PROVENANCE_FIELDS if field in all_fields]
        destination = revision_output / filename
        with destination.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=import_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows({field: row.get(field, "") for field in import_fields} for row in revision_rows)
        business_dates = sorted(row["business_date"] for row in revision_rows)
        sidecar = {
            "source_file": f"dataset/data/04_经营事实增强/lifecycle_revisions/{filename}",
            "import_file": f"dataset/data/04_经营事实增强/import_ready/lifecycle_revisions/{filename}",
            "provenance": "synthetic_revision_fixture",
            "source_payload": {
                "shop": "<目标测试店铺 UUID>",
                "name": f"边界夹具：{kind} 修订/撤销",
                "kind": kind,
                "is_primary": True,
                "grain": grain,
                "column_mapping": {field: field for field in core_fields},
                "ignored_columns": [field for field in import_fields if field not in core_fields],
                "coverage_basis": "business_date",
                **({"return_event": "received"} if kind == "returns" else {}),
            },
            "upload_payload": {
                "coverage_start": min(business_dates),
                "coverage_end": max(business_dates),
                "generated_at": manifest["revision_and_retraction_fixtures"].get("late_arrival_generated_at", manifest["generated_at"]) if filename == "refund_late_arrival.csv" else manifest["generated_at"],
                "declared_complete": False,
                "declared_asins": sorted({row["child_asin"] for row in revision_rows}),
            },
            "warning": "每个修订文件须作为新批次依次上传到同一来源；不要把重复 business_key 合并到同一个 CSV。",
        }
        (destination.with_suffix(destination.suffix + ".meta.json")).write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        results.append({"file": str(destination), "rows": len(revision_rows), "kind": kind, "fixture": True})

    empty_source = data_dir / "boundary_cases" / "refunds_empty_report_v1.csv"
    empty_destination = output_dir / "refunds_empty_report_v1.csv"
    if empty_source.exists():
        empty_destination.write_bytes(empty_source.read_bytes())
        empty_meta = {
            "source_file": "dataset/data/04_经营事实增强/boundary_cases/refunds_empty_report_v1.csv",
            "import_file": "dataset/data/04_经营事实增强/import_ready/refunds_empty_report_v1.csv",
            "source_payload": {
                "shop": "<目标测试店铺 UUID>",
                "name": "边界夹具：完整零退款报表",
                "kind": "refunds",
                "is_primary": True,
                "grain": "product_record",
                "column_mapping": {field: field for field in ["child_asin", "seller_sku", "fulfillment", "business_date", "record_id", "order_line_id", "transaction_chain_id", "amount", "currency", "quantity", "occurred_at"]},
                "ignored_columns": [],
                "coverage_basis": "business_date",
                "empty_report_means_zero": True,
            },
            "upload_payload": {
                "coverage_start": (parse_date(manifest["coverage"]["end"]) + timedelta(days=1)).isoformat(),
                "coverage_end": (parse_date(manifest["coverage"]["end"]) + timedelta(days=1)).isoformat(),
                "generated_at": manifest["generated_at"],
                "declared_complete": True,
                "declared_asins": manifest["files"]["refunds_v1.csv"]["asins"],
            },
            "warning": "仅在业务确认‘完整空报表表示零事件’后使用；不得用它覆盖已有历史退款。",
        }
        (empty_destination.with_suffix(empty_destination.suffix + ".meta.json")).write_text(
            json.dumps(empty_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (output_dir / "import_manifest.json").write_text(
        json.dumps({"generated_from": "manifest.json", "files": results}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 Django 导入所需的纯契约 CSV 与 sidecar")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    results = convert(args.data_dir, args.output_dir)
    print(json.dumps({"files": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
