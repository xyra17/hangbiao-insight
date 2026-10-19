"""Export actual traces and bind independent judgments. Never reads gold."""
import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from insight.investigation.evaluation import ADAPTER_VERSION, adapt_prediction, load_adjudications, write_template  # noqa: E402

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-report", type=Path, required=True)
    parser.add_argument("--adjudications", type=Path)
    parser.add_argument("--write-template", type=Path)
    parser.add_argument("--predictions-csv", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runtime = json.loads(args.runtime_report.read_text())
    traces = runtime["trajectories"]
    judgments = load_adjudications(args.adjudications, traces)
    pairs = [adapt_prediction(trace, judgments.get(trace["scenario_id"])) for trace in traces]
    if args.write_template:
        write_template(args.write_template, traces)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"adapter_version": ADAPTER_VERSION, "runtime_report_sha256": hashlib.sha256(args.runtime_report.read_bytes()).hexdigest(),
                                      "scenario_ids": sorted(trace["scenario_id"] for trace in traces), "predictions": [p for p, _ in pairs],
                                      "provenance": [meta for _, meta in pairs]}, ensure_ascii=False, indent=2) + "\n")
    if args.predictions_csv:
        args.predictions_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.predictions_csv.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(pairs[0][0]) if pairs else [])
            writer.writeheader()
            writer.writerows(prediction for prediction, _ in pairs)
    print(json.dumps({"trace_count": len(traces), "judgment_count": len(judgments), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
