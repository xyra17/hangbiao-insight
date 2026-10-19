#!/usr/bin/env python3
"""Validate the generated business-fact augmentation package.

The validator is intentionally independent of Django so it can run before a
database exists.  It checks row counts, dates, unique business keys, provenance
flags, boundary coverage, order-line links, and the intentionally invalid or
empty fixtures.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR / "data" / "04_经营事实增强"


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def add_check(checks: list[dict[str, object]], name: str, ok: bool, detail: object) -> None:
    checks.append({"check": name, "status": "passed" if ok else "failed", "detail": detail})


def validate(data_dir: Path = DEFAULT_DATA_DIR, report_path: Path | None = None) -> dict[str, object]:
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    expected_result = json.loads((data_dir / "expected_results.json").read_text(encoding="utf-8"))
    checks: list[dict[str, object]] = []
    file_stats: dict[str, object] = {}
    all_boundary_counts: Counter[str] = Counter()
    order_line_ids: set[str] = set()
    support_review_ids: set[str] = set()

    for filename, info in manifest["files"].items():
        path = data_dir / filename
        file_rows = rows(path)
        expected_count = int(info["rows"])
        source_expected = int(json.loads((data_dir / "expected_results.json").read_text(encoding="utf-8"))["row_counts"][filename])
        add_check(checks, f"{filename}:row_count", len(file_rows) == expected_count == source_expected, {"actual": len(file_rows), "manifest_expected": expected_count, "pinned_expected": source_expected})
        manifest_complete = all(info.get(field) for field in ("source", "provenance", "license", "generated_at", "version", "coverage_start", "coverage_end", "intended_use"))
        role_fields = set(info.get("real_fields", [])) | set(info.get("derived_fields", [])) | set(info.get("synthetic_fields", []))
        add_check(checks, f"{filename}:manifest_provenance", manifest_complete and set(info["contract_fields"]) <= role_fields, {"source": info.get("source"), "provenance": info.get("provenance"), "license": info.get("license"), "unclassified_fields": sorted(set(info["contract_fields"]) - role_fields)})
        dates: list[date] = []
        invalid_dates: list[str] = []
        for row in file_rows:
            try:
                dates.append(parse_date(row.get("business_date", "")))
            except (TypeError, ValueError):
                invalid_dates.append(row.get("business_date", ""))
        add_check(checks, f"{filename}:business_date_parse", not invalid_dates, invalid_dates[:5])
        if dates:
            date_bounds = {"min": min(dates).isoformat(), "max": max(dates).isoformat()}
            add_check(
                checks,
                f"{filename}:date_coverage",
                date_bounds["min"] == info["coverage_start"] and date_bounds["max"] == info["coverage_end"],
                {"actual": date_bounds, "expected": {"min": info["coverage_start"], "max": info["coverage_end"]}},
            )
        key_fields = ["child_asin", "business_date"] if info["grain"].endswith("day") else ["record_id"]
        keys = ["|".join(row.get(field, "") for field in key_fields) for row in file_rows]
        duplicates = sorted(key for key, count in Counter(keys).items() if count > 1 and key.strip("|") != "")
        # A same record_id may appear in a later revision file, but never twice
        # in one publishable CSV.
        add_check(checks, f"{filename}:unique_business_key", not duplicates, duplicates[:5])
        missing_flags = [
            row.get("source_row_id", "")
            for row in file_rows
            if not row.get("provenance_class")
            or row.get("is_real") not in {"true", "false"}
            or row.get("is_derived") not in {"true", "false"}
            or row.get("is_synthetic") not in {"true", "false"}
            or not row.get("source_reference")
        ]
        add_check(checks, f"{filename}:provenance_flags", not missing_flags, missing_flags[:5])
        boundary_counts = Counter(row.get("boundary_case", "") for row in file_rows if row.get("boundary_case"))
        all_boundary_counts.update(boundary_counts)
        all_boundary_counts.update(row.get("currency_case", "") for row in file_rows if row.get("currency_case"))
        if filename == "support_cases_v1.csv":
            missing_support_refs = [row.get("record_id", "") for row in file_rows if not row.get("synthetic_from_review")]
            add_check(checks, f"{filename}:synthetic_from_review", not missing_support_refs, missing_support_refs[:5])
            support_review_ids = {row.get("synthetic_from_review", "") for row in file_rows}
            categories = {row.get("case_category", "") for row in file_rows}
            required_categories = {"product_failure", "performance_issue", "compatibility_issue", "safety_issue", "logistics_issue"}
            add_check(checks, f"{filename}:required_categories", required_categories <= categories, sorted(required_categories - categories))
        if filename == "orders_v1.csv":
            order_line_ids = {row.get("record_id", "") for row in file_rows}
        if filename in {"refunds_v1.csv", "returns_v1.csv", "support_cases_v1.csv"}:
            links = [row.get("order_line_id", "") for row in file_rows if row.get("order_line_id")]
            missing_links = sorted(set(links) - order_line_ids) if order_line_ids else links
            # Order file is iterated before event files in the manifest.  Keep
            # the check here and repeat it below after all files are loaded.
            file_stats.setdefault("pending_links", {})[filename] = missing_links
        file_stats[filename] = {"rows": len(file_rows), "boundary_counts": dict(boundary_counts), "asins": sorted({row.get("child_asin", "") for row in file_rows})}

    for filename in ("refunds_v1.csv", "returns_v1.csv", "support_cases_v1.csv"):
        links = []
        for row in rows(data_dir / filename):
            if row.get("order_line_id"):
                links.append(row["order_line_id"])
        missing_links = sorted(set(links) - order_line_ids)
        add_check(checks, f"{filename}:order_line_linkage", not missing_links, missing_links[:10])

    required_boundaries = {
        "revision",
        "retraction_candidate",
        "late_arrival",
        "partial_refund",
        "full_refund",
        "reversal",
        "cross_month_return",
        "unknown_status_code",
        "unknown_currency",
    }
    add_check(checks, "boundary:required_cases", required_boundaries <= set(all_boundary_counts), sorted(required_boundaries - set(all_boundary_counts)))
    missing_boundary_minimums = {
        name: {"actual": all_boundary_counts.get(name, 0), "minimum": minimum}
        for name, minimum in expected_result["required_boundary_counts_minimum"].items()
        if all_boundary_counts.get(name, 0) < minimum
    }
    add_check(checks, "boundary:pinned_minimum_counts", not missing_boundary_minimums, missing_boundary_minimums)
    invalid_fixtures = manifest["boundary_cases"]["invalid_fixtures"]
    fixture_results = []
    for item in invalid_fixtures:
        path = data_dir / "boundary_cases" / item["file"]
        fixture_rows = rows(path)
        fixture_results.append({"file": item["file"], "rows": len(fixture_rows), "expected_error": item["expected_error"]})
    add_check(checks, "boundary:invalid_fixtures_present", all(item["rows"] > 0 for item in fixture_results), fixture_results)
    empty_path = data_dir / "boundary_cases" / "refunds_empty_report_v1.csv"
    empty_rows = rows(empty_path)
    add_check(checks, "boundary:empty_report", len(empty_rows) == 0, {"rows": len(empty_rows), "requires": "empty_report_means_zero=true"})

    revision_path = data_dir / "lifecycle_revisions"
    sales_v1, sales_v2 = rows(revision_path / "sales_traffic_revision_v1.csv"), rows(revision_path / "sales_traffic_revision_v2.csv")
    same_sales_key = bool(sales_v1 and sales_v2) and (sales_v1[0]["child_asin"], sales_v1[0]["business_date"]) == (sales_v2[0]["child_asin"], sales_v2[0]["business_date"])
    changed_sales_payload = bool(sales_v1 and sales_v2) and sales_v1[0]["sessions"] != sales_v2[0]["sessions"]
    add_check(checks, "revision:sales_same_key_changed_payload", same_sales_key and changed_sales_payload, {"v1": sales_v1[0] if sales_v1 else None, "v2": sales_v2[0] if sales_v2 else None})
    return_versions = [rows(revision_path / name) for name in ("returns_requested_v1.csv", "returns_received_v2.csv", "returns_cancelled_v3.csv")]
    transition_ids = [version[0]["record_id"] for version in return_versions if version]
    transition_statuses = [version[0]["status"] for version in return_versions if version]
    add_check(checks, "revision:return_requested_received_cancelled", len(set(transition_ids)) == 1 and transition_statuses == ["requested", "received", "cancelled"], {"record_ids": transition_ids, "statuses": transition_statuses})
    late_info = manifest["revision_and_retraction_fixtures"]
    late_rows = rows(revision_path / "refund_late_arrival.csv")
    late_day = parse_date(late_info["late_arrival_business_date"])
    from datetime import datetime
    generated_day = datetime.fromisoformat(late_info["late_arrival_generated_at"].replace("Z", "+00:00")).date()
    late_delta = (generated_day - late_day).days
    add_check(checks, "boundary:late_batch_arrival", bool(late_rows) and late_delta >= 30, {"business_date": late_day.isoformat(), "generated_at": late_info["late_arrival_generated_at"], "arrival_lag_days": late_delta})
    retraction_rows = rows(revision_path / "order_retraction_candidate.csv")
    add_check(checks, "revision:retraction_fixture", len(retraction_rows) == expected_result["fixtures"]["retraction_candidate_rows"] and bool(late_info.get("retraction_steps")), {"rows": len(retraction_rows), "steps": late_info.get("retraction_steps")})

    passed = all(check["status"] == "passed" for check in checks)
    report = {
        "status": "passed" if passed else "failed",
        "manifest_version": manifest["manifest_version"],
        "generated_at": manifest["generated_at"],
        "checked_at": manifest["generated_at"],
        "checks": checks,
        "file_stats": file_stats,
        "boundary_counts": dict(sorted(all_boundary_counts.items())),
        "fixture_results": fixture_results,
        "support_review_reference_count": len(support_review_ids),
        "expected_row_counts": expected_result["row_counts"],
    }
    report_path = report_path or data_dir / "validation" / "validation_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (report_path.parent / "validation_results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["check", "status", "detail"])
        writer.writeheader()
        for check in checks:
            writer.writerow({"check": check["check"], "status": check["status"], "detail": json.dumps(check["detail"], ensure_ascii=False)})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="校验经营事实增强包")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    report = validate(args.data_dir, args.report)
    print(json.dumps({"status": report["status"], "checks": len(report["checks"]), "failed": [check["check"] for check in report["checks"] if check["status"] == "failed"]}, ensure_ascii=False, indent=2))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
