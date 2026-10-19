"""Free cumulative budget/receipt/circuit tests. No provider construction occurs."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("quality_round_guard_test", Path(__file__).resolve().parents[2] / "scripts/quality_round_guard.py")
guard_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard_module)


def plan(tmp_path):
    value = {"model": "deepseek-flash", "runtime_hashes": {}, "budget": {"total_upper_usd": ".01", "max_calls": 1,
        "investigation_call_units": 1, "calls": {"classification": {"upper_usd": ".01", "request_bytes": 8000, "output_tokens": 1200}}}}
    (tmp_path / "plan.json").write_text(json.dumps(value))
    return {"model": "deepseek-flash", "reasoning": {"effort": "none"}, "max_output_tokens": 1200,
            "input": [{"role": "system", "content": "protocol"}, {"role": "user", "content": '{}'}],
            "text": {"format": {"name": "review_semantic_extraction", "strict": True,
                     "schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}}}


def approve(tmp_path):
    offline = tmp_path / "offline"
    offline.mkdir()
    (offline / "summary.json").write_text(json.dumps({"status": "offline_protocol_passed", "real_dispatches": 0,
        "plan_sha256": guard_module.file_sha(tmp_path / "plan.json")}))
    (offline / "seal.json").write_text(json.dumps({"summary.json": guard_module.file_sha(offline / "summary.json")}))
    (tmp_path / "approval.json").write_text(json.dumps({"approved": True, "transfer_confirmed": True,
        "plan_sha256": guard_module.file_sha(tmp_path / "plan.json"), "max_total_usd": ".01",
        "authorization_reference": "controlled-fixture-not-paid", "processing_region": "controlled"}))


def test_unapproved_round_cannot_construct_provider_or_create_paid_directory(tmp_path):
    plan(tmp_path)
    (tmp_path / "approval.json").write_text('{}')
    with pytest.raises(ValueError, match="fresh_whole_round"):
        guard_module.RoundGuard(tmp_path, paid=True, constructor=lambda **kw: pytest.fail("provider constructed"))
    assert not (tmp_path / "paid").exists()


def test_cumulative_cap_and_no_redispatch(tmp_path):
    request = plan(tmp_path)
    guard = guard_module.RoundGuard(tmp_path, paid=False, constructor=lambda **kw: pytest.fail("provider constructed"))
    guard.group = "classification:development"
    client = guard.client(max_retries=0, base_url="https://api.deepseek.com")
    assert client.responses.create(**request).status == "completed"
    with pytest.raises(ValueError, match="budget_exhausted"):
        client.responses.create(**request)
    assert len(guard.receipts) == 1
    summary = guard.finish({"classification": 1})
    assert summary["real_dispatches"] == 0 and summary["reserved_usd"] == "0.01"
    with pytest.raises(FileExistsError):
        guard_module.RoundGuard(tmp_path, paid=False, constructor=None)


def test_supplier_failure_retains_reservation_and_opens_circuit(tmp_path):
    request = plan(tmp_path)
    approve(tmp_path)
    dispatches = []
    def create(**kwargs):
        dispatches.append(kwargs)
        raise TimeoutError("controlled failure")
    guard = guard_module.RoundGuard(tmp_path, paid=True, constructor=lambda **kw: SimpleNamespace(responses=SimpleNamespace(create=create)))
    guard.group = "classification:development"
    client = guard.client(max_retries=0, base_url="https://api.deepseek.com")
    with pytest.raises(TimeoutError):
        client.responses.create(**request)
    with pytest.raises(RuntimeError, match="circuit_open"):
        client.responses.create(**request)
    assert len(dispatches) == 1 and str(guard.reserved) == "0.01"
    assert json.loads((tmp_path / "paid/error-0001.json").read_text())["reservation_retained"]


def test_received_but_missing_usage_remains_unknown(tmp_path):
    request = plan(tmp_path)
    approve(tmp_path)
    response = SimpleNamespace(status="completed", model_dump=lambda **kw: {"status": "completed", "usage": None})
    guard = guard_module.RoundGuard(tmp_path, paid=True, constructor=lambda **kw: SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: response)))
    guard.group = "classification:development"
    with pytest.raises(RuntimeError, match="usage_incomplete"):
        guard.client(max_retries=0, base_url="https://api.deepseek.com").responses.create(**request)
    assert (tmp_path / "paid/response-0001.json").exists() and str(guard.reserved) == "0.01"


def test_runtime_contract_failure_seals_partial_and_opens_whole_round_circuit(tmp_path):
    request = plan(tmp_path)
    guard = guard_module.RoundGuard(tmp_path, paid=False, constructor=None)
    guard.group = "classification:development"
    client = guard.client(max_retries=0, base_url="https://api.deepseek.com")
    client.responses.create(**request)
    summary = guard.finish({"classification": 0}, error="controlled_adapter_contract_failure")
    assert summary["status"] == "partial" and summary["stopped"] == "runtime_failure"
    assert summary["reserved_usd"] == "0.01" and summary["quality_accepted"] is False
    assert (tmp_path / "offline/seal.json").exists()
    with pytest.raises(RuntimeError, match="whole_round_circuit_open"):
        client.responses.create(**request)
    assert len(guard.receipts) == 1
