from datetime import date, timedelta

import pytest

from insight.metrics.daily import calculate_daily
from insight.metrics.facts import source_manifest
from tests_v2.test_metrics import daily_rows, report

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("units,sessions", [(0, 100), (0, 0), (None, 100), (10, None)])
def test_zero_or_unknown_baseline_is_not_interchangeable(metric_scope, units, sessions):
    _, shops = metric_scope
    start = date(2026, 8, 1)
    rows = daily_rows(start, 10)
    for row in rows[:28]:
        row["ordered_units"], row["sessions"] = units, sessions
    report(metric_scope, "sales_traffic", rows, start, date(2026, 9, 4))
    result = calculate_daily(source_manifest(shops[0]), "B000TEST01", date(2026, 9, 5))
    assert result["baseline"]["ordered_units"] == (units * 28 if units is not None else None)
    assert result["baseline"]["sessions"] == (sessions * 28 if sessions is not None else None)
    expected = (
        "missing" if units is None or sessions is None else "not_applicable" if sessions == 0 else "ready"
    )
    assert result["baseline"]["ratio_status"] == expected
    assert not any(s["metric"] == "unit_session_percent" for s in result["signals"])
    if units == 0 or units is None:
        assert not any(s["metric"] == "sales_units" for s in result["signals"])


@pytest.mark.parametrize("today", [date(2026, 3, 9), date(2026, 11, 2)])
def test_day_windows_across_dst_still_contain_seven_and_twenty_eight_days(metric_scope, today):
    _, shops = metric_scope
    start = today - timedelta(days=35)
    report(metric_scope, "sales_traffic", daily_rows(start), start, today - timedelta(days=1))
    result = calculate_daily(source_manifest(shops[0]), "B000TEST01", today)
    assert result["status"] == "ready"
    assert result["baseline"]["days"] == 28 and result["current"]["days"] == 7
    assert result["baseline"]["ordered_units"] == 560 and result["current"]["ordered_units"] == 28
    assert len(result["baseline"]["raw_row_ids"]) == 28
    assert len(result["current"]["raw_row_ids"]) == 7


def test_new_product_with_only_current_window_is_partial_not_zero_baseline(metric_scope):
    _, shops = metric_scope
    rows = daily_rows(date(2026, 8, 1))[28:]
    report(metric_scope, "sales_traffic", rows, date(2026, 8, 29), date(2026, 9, 4))
    result = calculate_daily(source_manifest(shops[0]), "B000TEST01", date(2026, 9, 5))
    assert result["status"] == "partial" and result["signals"] == []
    assert result["baseline"]["ordered_units"] is None
    assert not result["baseline"]["complete"]
