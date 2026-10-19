"""Bind a real-API production-gateway fixture to CLI scoring and a failing gate.

Consumes only the explicitly controlled fixture produced by
 tests_v2/test_investigation_output.py. This is protocol QA, never model quality.
"""
import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from insight.investigation.evaluation import write_template  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.input.read_text())
    if fixture.get("provenance") != "controlled_provider_fixture" or fixture.get("real_model_quality") != "not_established":
        raise ValueError("explicit controlled fixture required")
    traces = fixture["traces"]
    if not all(any(item.get("planner") == "controlled-provider-fixture-v1" and item.get("conclusion", {}).get("schema_version") == "agent-final-output-v3" for item in trace["result"]["decisions"] if isinstance(item.get("conclusion"), dict)) for trace in traces):
        raise ValueError("production structured protocol traces required")
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    runtime = output / "runtime.json"
    runtime.write_text(json.dumps({"trajectories": traces, "request_count": len(traces), "pair_count": 1,
                                   "output_schema_version": "investigation-output-v1", "provenance": "controlled_production_gateway"}, ensure_ascii=False, indent=2))
    judgments = output / "adjudications.csv"
    write_template(judgments, traces)
    rows = list(csv.DictReader(judgments.open()))
    # Expected values are authored only by the development fixture, after its run.
    expected = {variant["scenario_id"]: variant for pair in fixture["scoring"]["pairs"] for variant in pair["variants"]}
    for row in rows:
        row.update(expected[row["scenario_id"]]["expected"], reviewer="controlled-protocol-fixture",
                   reviewed_at="2026-10-07T00:00:00Z", adjudication_kind="controlled_fixture")
    with judgments.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    pack = output / "controlled-pack"
    pack.mkdir(exist_ok=True)
    gold = [{"scenario_id": key, "pair_id": "controlled-development-pair", "evidence_variant": item["evidence_variant"], **item["expected"]} for key, item in expected.items()]
    with (pack / "paired_investigation_scenarios.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=gold[0].keys())
        writer.writeheader()
        writer.writerows(gold)
    retrieval = output / "retrieval.json"
    retrieval.write_text('{}')  # no retrieval quality claim from this tiny protocol fixture
    adapted, score, bundle, gate = [output / f"{name}.json" for name in ("adapted", "score", "bundle", "gate")]
    commands = [
        ("adapt_investigation_outputs.py", ["--runtime-report", runtime, "--adjudications", judgments, "--output", adapted], 0),
        ("score_paired_investigations.py", ["--runtime-report", runtime, "--adapted", adapted, "--pack-dir", pack, "--output", score], 0),
        ("quality_gate.py", ["--retrieval", retrieval, "--paired", runtime, "--adapted", adapted, "--scoring", score, "--bundle-output", bundle, "--output", gate], 1),
    ]
    outcomes = []
    for name, flags, required in commands:
        command = [sys.executable, str(ROOT / "dataset" / name), *map(str, flags)]
        process = subprocess.run(command, capture_output=True, text=True)
        (output / f"{name}.log").write_text(process.stdout + process.stderr)
        outcomes.append({"command": command, "exit_code": process.returncode, "expected_exit": required})
        if process.returncode != required:
            raise RuntimeError(f"Unexpected exit for {name}; inspect log")
    result = {"status": "protocol_chain_passed", "real_model_quality": "not_established", "commands": outcomes,
              "score": json.loads(score.read_text())["status"], "gate": json.loads(gate.read_text())["status"],
              "independent_adjudication_complete": json.loads(bundle.read_text())["independent_adjudication_complete"]}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
