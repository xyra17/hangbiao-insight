"""Read-only provenance, grain and evaluation leakage inventory for release inputs."""
import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    gold = ROOT / "dataset/data/03_黄金评论集"
    split = rows(gold / "Golden语义评测切分_v1_0.csv")
    groups = defaultdict(set)
    for row in split:
        groups[row["split"]].add(row["product_id"])
    overlaps = {a+":"+b: sorted(groups[a] & groups[b]) for a in groups for b in groups if a < b}
    runtime = ROOT / "dataset/data/07_S4_S6独立评测_v1/runtime_corpus_v4"
    manifest = json.loads((runtime / "corpus_manifest.json").read_text())
    matches = {name: hashlib.sha256((runtime/name).read_bytes()).hexdigest() == value["sha256"] for name, value in manifest["files"].items()}
    forbidden = {"topic", "qrels", "answerable", "expected_route", "expected_behavior", "expected_next_step"}
    clean = {}
    for name in manifest["files"]:
        records = rows(runtime/name)
        clean[name] = not any(key in forbidden or key.startswith("expected_") for key in records[0])
    report = {"status": "passed" if all(matches.values()) and all(clean.values()) and len({r["review_id"] for r in split}) == len(split) else "failed",
              "scope": "inventory correctness only; independent semantic truth and fresh blinded product/time test not established",
              "02_to_04": [
                  {"02": "product_id,date,order_count", "04": "child_asin,business_date,order_count (sales_traffic)", "unit": "orders/site day", "difference": "02 product identifiers align to child ASIN; synthetic counts can be compared for compatible dates, not individual transactions"},
                  {"02": "refund_count", "04": "positive refund transaction count / distinct affected mature orders", "unit": "different denominators", "difference": "02 daily constructed count has no transaction chain or maturity; cannot equate with 04 refund transactions or cohort numerator"},
                  {"02": "refund_rate", "04": "mature cohort rate or occurrence count", "unit": "02 count/order; cohort affected mature orders/mature shipped orders", "difference": "daily refund/order ratio is a constructed baseline, not a defect rate or maturity-adjusted cohort"},
                  {"02": "source,version,scenario_id,is_injected", "04": "source_reference,generation_version,scenario_id,provenance_class", "unit": "provenance", "difference": "both synthetic/derived; no independent financial signoff"}],
              "03": {"files_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in gold.iterdir() if p.is_file()}, "rows": len(split), "split_counts": dict(Counter(r["split"] for r in split)), "unique_review_ids": len({r["review_id"] for r in split}), "product_overlap": overlaps,
                     "split_boundary": "legacy review_id split, overlapping products; not a new product/time blinded evaluation", "label_signoff": "pending independent review, including scarce secondary topics"},
              "07_runtime": {"file_hashes_match": matches, "no_gold_columns": clean, "manifest_sha256": hashlib.sha256((runtime/"corpus_manifest.json").read_bytes()).hexdigest(), "source_nature": "synthetic; gold read only after runtime in scorer", "fresh_blind_test": False}}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
