"""Prepare a fresh unapproved synthetic diagnostic and immutable runtime snapshot."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path


def prepare(output, inputs):
    root = Path(__file__).resolve().parents[1]
    output, inputs = output.resolve(), inputs.resolve()
    if output.exists() or not output.is_relative_to(root / ".local/acceptance-artifacts"):
        raise ValueError("unique acceptance output required")
    data = json.loads(inputs.read_text())
    if data.get("source_nature") != "independent_synthetic_development" or data.get("contains_labels") is not False or len(data["cases"]) != 4:
        raise ValueError("exact four-case synthetic development scope required")
    for case in data["cases"]:
        if set(case) != {"case_id", "pair_id", "query", "asin", "start", "end", "documents"}:
            raise ValueError("only runtime case fields permitted")
        for doc in case["documents"]:
            if set(doc) != {"record_id", "asin", "date", "text"}:
                raise ValueError("only original synthetic document fields permitted")
    output.mkdir(parents=True)
    shutil.copyfile(inputs, output / "inputs.json")
    files = [path for directory in ["backend/app", "backend/insight", "backend/config"]
             for path in (root / directory).rglob("*.py")]
    files += [root / "backend/tests_v2/test_paid_development_diagnostic.py", Path(__file__).resolve()]
    hashes = {}
    for path in files:
        name = str(path.relative_to(root))
        destination = output / "snapshot" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (output / "runtime-source.json").write_text(json.dumps(hashes, indent=2))
    (output / "approval.json").write_text(json.dumps({"approved": False, "hard_cap_usd": "0.11",
        "inputs_sha256": hashlib.sha256(inputs.read_bytes()).hexdigest(), "authorization_reference": None,
        "scope": "four synthetic development investigations and four retrieval queries; no gold or business data",
        "max_external_calls": 20, "max_call_units_per_investigation": 4, "planner_cap_usd": "0.0060576",
        "relevance_cap_usd": "0.0029760", "whole_plan_ceiling_usd": "0.1088256", "past_budget_reused": False}, indent=2))
    (output / "plan.json").write_text(json.dumps({"status": "prepared_not_approved_not_run",
        "runtime_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "mode": "development_only_not_blind_acceptance", "model": "deepseek-flash",
        "requires": ["fresh explicit budget and synthetic transfer authorization", "offline complete-graph preflight",
                     "rates reconfirmed", "seal before separate adjudication"],
        "runner": "backend/tests_v2/test_paid_development_diagnostic.py",
        "run_env": {"QUALITY_DEVELOPMENT_RUN": str(output), "QUALITY_DEVELOPMENT_PAID": "0"}}, indent=2))
    environment = {
        "QUALITY_DEVELOPMENT_RUN": str(output), "INSIGHT_ENABLED_MODEL_PROVIDERS": "deepseek",
        "INVESTIGATION_MODEL": "deepseek-flash", "EVIDENCE_RELEVANCE_MODEL": "deepseek-flash",
        "INVESTIGATION_REASONING_EFFORT": "none", "EVIDENCE_RELEVANCE_REASONING_EFFORT": "none",
        "INVESTIGATION_MAX_REQUEST_BYTES": "8192", "INVESTIGATION_MAX_OUTPUT_TOKENS": "3000",
        "INVESTIGATION_MAX_CALL_COST": "0.0060576", "INVESTIGATION_MAX_RUN_COST": "0.0242304",
        "INVESTIGATION_MAX_EVAL_COST": "0.1088256", "INVESTIGATION_COST_CURRENCY": "USD",
        "INVESTIGATION_INPUT_COST_PER_MILLION": "0.30", "INVESTIGATION_OUTPUT_COST_PER_MILLION": "1.20",
        "EVIDENCE_RELEVANCE_ENABLED": "1", "EVIDENCE_RELEVANCE_PROVIDER": "deepseek",
        "EVIDENCE_RELEVANCE_MAX_REQUEST_BYTES": "4096", "EVIDENCE_RELEVANCE_MAX_OUTPUT_TOKENS": "1200",
        "EVIDENCE_RELEVANCE_MAX_CANDIDATES": "2", "EVIDENCE_RELEVANCE_MAX_COST": "0.0029760",
        "EVIDENCE_RELEVANCE_COST_CURRENCY": "USD", "EVIDENCE_RELEVANCE_INPUT_COST_PER_MILLION": "0.30",
        "EVIDENCE_RELEVANCE_OUTPUT_COST_PER_MILLION": "1.20",
        "INVESTIGATION_APPROVAL_REFERENCE": "offline-preparation-no-paid-authority",
        "EVIDENCE_RELEVANCE_APPROVAL_REFERENCE": "offline-preparation-no-paid-authority",
        "INVESTIGATION_REGION": "offline-no-transfer", "EVIDENCE_RELEVANCE_REGION": "offline-no-transfer",
    }
    (output / "diagnostic.env").write_text("\n".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in environment.items()) + "\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, default=Path(__file__).resolve().parents[1] / "backend/tests_v2/fixtures/quality_development_inputs.json")
    args = parser.parse_args()
    print(prepare(args.output, args.inputs))
