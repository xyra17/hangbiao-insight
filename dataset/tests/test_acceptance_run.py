import importlib.util
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "prepare_acceptance_run.py"
SPEC = importlib.util.spec_from_file_location("prepare_acceptance_run", SCRIPT)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def test_frozen_replay_records_an_explicit_historical_clock():
    assert RUNNER.resolve_dates("frozen-replay", date(2026, 9, 30), date(2026, 10, 2)) == (
        date(2026, 9, 30),
        "frozen",
    )
    assert RUNNER.resolve_dates("frozen-replay", None, date(2026, 10, 2)) == (
        date(2026, 10, 1),
        "frozen",
    )


def test_current_demo_rejects_a_stale_or_future_as_of():
    assert RUNNER.resolve_dates("current-demo", None, date(2026, 10, 2)) == (
        date(2026, 10, 2),
        "current",
    )
    with pytest.raises(ValueError, match="must match"):
        RUNNER.resolve_dates("current-demo", date(2026, 10, 1), date(2026, 10, 2))


def test_frozen_replay_rejects_future_business_dates():
    with pytest.raises(ValueError, match="future"):
        RUNNER.resolve_dates("frozen-replay", date(2026, 10, 3), date(2026, 10, 2))


def test_file_hash_is_stable_and_sha256_sized(tmp_path):
    sample = tmp_path / "fixture.csv"
    sample.write_text("fixture\n", encoding="utf-8")
    first = RUNNER.sha256(sample)
    assert first == RUNNER.sha256(sample)
    assert len(first) == 64
