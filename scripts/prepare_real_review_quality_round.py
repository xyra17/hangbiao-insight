"""Freeze a real-review quality candidate and bind the user's new authorization."""

import argparse
import csv
import json
import shutil
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from quality_round_guard import file_sha, save
from run_real_review_business_validation import INVESTIGATIONS, TOPICS

ROOT = Path(__file__).resolve().parents[1]
INVESTIGATION_START = "2022-09-03"
INVESTIGATION_END = "2023-09-02"
QUALITY_QUERIES = tuple(
    "查找充电时需要反复调整手机位置的评论" if query == "总结评论" else query
    for query in TOPICS
)


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def configure_planner_budget(template, size):
    """Recompute all worst-case ceilings; never reuse a previous approval."""
    if not 1 <= size <= 65536:
        raise ValueError("planner request limit must be between 1 and 65536 bytes")
    budget = template["budget"]
    planner = budget["calls"]["planner"]
    planner["request_bytes"] = size
    input_rate = Decimal(budget["input_usd_per_million"])
    output_rate = Decimal(budget["output_usd_per_million"])
    planner["upper_usd"] = str((Decimal(size) * input_rate + Decimal(planner["output_tokens"]) * output_rate) / 1000000)
    per_run = budget["investigation_call_units"] * max(Decimal(budget["calls"][k]["upper_usd"]) for k in ("planner", "relevance"))
    total = 48 * per_run + 30 * Decimal(budget["calls"]["relevance"]["upper_usd"]) + 300 * Decimal(budget["calls"]["classification"]["upper_usd"])
    cap = str(total.quantize(Decimal(".01"), rounding=ROUND_CEILING))
    budget.update(per_investigation_upper_usd=str(per_run), calculated_upper_usd=str(total), total_upper_usd=cap,
                  past_approved_balance_reused=False)
    template["environment"].update(INVESTIGATION_MAX_REQUEST_BYTES=str(size),
        INVESTIGATION_MAX_CALL_COST=planner["upper_usd"], INVESTIGATION_MAX_RUN_COST=str(per_run), INVESTIGATION_MAX_EVAL_COST=cap)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--template-plan", type=Path, required=True)
    parser.add_argument("--semantic-prompt", choices=["semantic-prompt-v1.2", "semantic-prompt-v1.3", "semantic-prompt-v1.4", "semantic-prompt-v1.5", "semantic-prompt-v1.6", "semantic-prompt-v1.7"], default="semantic-prompt-v1.2")
    parser.add_argument("--round-budget-cap", type=Decimal, help="Hard per-round cap, may be below worst full coverage cost")
    parser.add_argument("--classification-request-bytes", type=int)
    parser.add_argument("--temperature", type=float, help="Explicit structured response sampling temperature, 0..2")
    parser.add_argument("--protocol", choices=["spans", "segments"], default="spans")
    parser.add_argument("--authorization-reference")
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--business-contract", choices=["legacy-v1", "native-v2", "native-v3"], default="legacy-v1")
    parser.add_argument("--planner-request-bytes", type=int,
                        help="Explicit planner wire limit, at most 65536 bytes; recalculates budget and creates a new candidate")
    args = parser.parse_args()
    if args.planner_request_bytes is not None and not 1 <= args.planner_request_bytes <= 65536:
        raise ValueError("planner request limit must be between 1 and 65536 bytes")
    package, run = args.package.resolve(), args.output.resolve()
    if run.exists() or not run.is_relative_to(ROOT / ".local/acceptance-artifacts"):
        raise ValueError("new owned run directory required")
    manifest = json.loads((package / "manifest.json").read_text())
    for name in ("prediction-input.jsonl", "review-provenance.jsonl"):
        if file_sha(package / name) != manifest["files"][name]:
            raise ValueError("real review package changed")
    if not manifest["gold_ids_and_normalized_text_excluded"] or not manifest["sample_normalized_text_unique"]:
        raise ValueError("real sample exclusion checks required")
    sampled = [json.loads(line) for line in (package / "review-provenance.jsonl").read_text().splitlines()]
    if len(sampled) != 300:
        raise ValueError("300 real reviews required")
    asins = sorted({row["sku"] for row in sampled})
    with (ROOT / "dataset/data/03_黄金评论集/黄金评论主表_v1_0.csv").open(encoding="utf-8-sig", newline="") as stream:
        excluded = {" ".join(row["text"].casefold().split()) for row in csv.DictReader(stream)}
    if file_sha(package / "historical-reviews.csv") != manifest["files"]["historical-reviews.csv"]:
        raise ValueError("historical reviews changed")
    with (package / "historical-reviews.csv").open(encoding="utf-8", newline="") as stream:
        rows = [row for row in csv.DictReader(stream)
                if INVESTIGATION_START <= str(datetime.fromisoformat(row["created_at"]).astimezone(ZoneInfo("America/Los_Angeles")).date()) <= INVESTIGATION_END
                and " ".join(row["text"].casefold().split()) not in excluded]
    # A declared recent business window, equal product/rating strata and text grouping;
    # neither lexical query matches nor model results select documents.
    grouped = {}
    for row in sorted(rows, key=lambda row: row["review_id"]):
        grouped.setdefault(" ".join(row["text"].casefold().split()), row)
    rows = list(grouped.values())
    docs = []
    for asin in asins:
        selected = []
        for low_rating in (False, True):
            bucket = [r for r in rows if r["sku"] == asin and (float(r["rating"]) <= 2) == low_rating]
            if len(bucket) < 30:
                raise ValueError("insufficient recent-window stratum")
            selected.extend(bucket[:30])
        for row in selected:
            timestamp = datetime.fromisoformat(row["created_at"])
            docs.append({"child_asin": asin, "seller_sku": asin + "-FBA", "fulfillment": "FBA",
                "business_date": str(timestamp.astimezone(ZoneInfo("America/Los_Angeles")).date()),
                "record_id": row["review_id"], "title": row["title"], "text": row["text"],
                "rating": int(float(row["rating"])), "language": "und", "collected_at": timestamp.isoformat()})
    start, end = INVESTIGATION_START, INVESTIGATION_END
    queries = [{"query_id": f"REAL-Q-{i * len(QUALITY_QUERIES) + j + 1:02}", "child_asin": asin,
                "query": query, "split": ("development", "validation", "test")[j % 3]}
               for i, asin in enumerate(asins) for j, query in enumerate(QUALITY_QUERIES)]
    investigations = [{"scenario_id": f"REAL-I-{i * len(INVESTIGATIONS) + j + 1:02}",
        "selected_child_asin": asin, "evidence_child_asin": asin,
        "query": f"{query}。日期范围 {start} 至 {end}。", "split": ("development", "validation", "test")[j % 3]}
        for i, asin in enumerate(asins) for j, query in enumerate(INVESTIGATIONS)]
    (run / "inputs").mkdir(parents=True)
    write_csv(run / "inputs/reviews.csv", docs)
    write_csv(run / "inputs/queries.csv", queries)
    write_csv(run / "inputs/investigations.csv", investigations)
    shutil.copyfile(package / "prediction-input.jsonl", run / "inputs/prediction-input.jsonl")
    shutil.copyfile(ROOT / "dataset/data/03_黄金评论集/主题分类体系_v1_1.json", run / "taxonomy.json")
    files = [p for directory in ("backend/app", "backend/insight", "backend/config")
             for p in (ROOT / directory).rglob("*.py")]
    files += [ROOT / "backend" / name for name in ("pyproject.toml", "uv.lock", "pytest-v2.ini")]
    files += [ROOT / "backend/tests_v2" / name for name in ("conftest.py", "test_s4_s6_runtime_acceptance.py", "test_real_review_quality_round.py")]
    files += [ROOT / "scripts/quality_round_guard.py", ROOT / "scripts/real_review_runtime_contract.py", ROOT / "scripts/real_review_stage_gate.py", ROOT / "scripts/audit_shared_business_contract.py", ROOT / "scripts/audit_planner_trajectory_wire.py",
              ROOT / "scripts/prepare_real_review_adjudication.py", ROOT / "scripts/score_real_review_quality.py", Path(__file__).resolve()]
    for path in files:
        target = run / "snapshot" / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    hashes = {str(p.relative_to(run)): file_sha(p) for p in run.rglob("*") if p.is_file()}
    template = json.loads(args.template_plan.read_text())
    if args.temperature is not None:
        if not 0 <= args.temperature <= 2:
            raise ValueError("temperature must be 0..2")
        template["environment"]["STRUCTURED_RESPONSE_TEMPERATURE"] = str(args.temperature)
    if args.planner_request_bytes is not None:
        configure_planner_budget(template, args.planner_request_bytes)
    if args.classification_request_bytes is not None:
        if not 8000 <= args.classification_request_bytes <= 24000:
            raise ValueError("classification request bytes must be 8000..24000")
        call = template["budget"]["calls"]["classification"]
        call["request_bytes"] = args.classification_request_bytes
        call["upper_usd"] = str((Decimal(call["request_bytes"]) * Decimal(".30") + Decimal(call["output_tokens"]) * Decimal("1.20")) / 1000000)
        configure_planner_budget(template, int(template["environment"]["INVESTIGATION_MAX_REQUEST_BYTES"]))
    if args.round_budget_cap is not None:
        if not 0 < args.round_budget_cap <= Decimal("15.00"):
            raise ValueError("round cap must be positive and <=15")
        template["budget"]["total_upper_usd"] = str(args.round_budget_cap)
        template["budget"]["full_coverage_worst_case_exceeds_cap"] = Decimal(template["budget"]["calculated_upper_usd"]) > args.round_budget_cap
        template["environment"]["INVESTIGATION_MAX_EVAL_COST"] = str(args.round_budget_cap)
    plan = {"version": "real-review-quality-round-v1", "status": "prepared_not_executed",
        "repository": str(ROOT), "runtime_hashes": hashes, "model": template["model"],
        "as_of": "2023-09-03T20:00:00+00:00", "semantic_prompt_version": args.semantic_prompt, "environment": template["environment"], "budget": template["budget"],
        "date_scope": {"start": start, "end": end},
        "source_manifest_sha256": file_sha(package / "manifest.json"),
        "scope": "300 real charger classifications, 30 real-review queries/120 original documents, 48 actual graph investigations",
        "full_39206_business_validation_is_separate": True, "gold_or_qrels_in_runtime": False,
        "independent_adjudications": 0, "quality_status": "not_run", "human_reviewer": "user",
        "sampling_boundary": "stratified development/validation material; no independent blind quality claim",
        "query_design_version": "specific-retrieval-queries-v2; summary-context query replaced without using qrels",
        "processing_region": "DeepSeek default; geographic location unverified"}
    if args.staged:
        plan["execution_stages"] = {"representative_id": investigations[0]["scenario_id"],
            "selection": "first existing original-input investigation; no labels/qrels used",
            "order": ["representative_investigation", "remaining_47_investigations", "30_retrieval_queries", "300_classifications"],
            "reuse_representative_once": True, "first_fault_stop": True, "automatic_retry": False,
            "representative_upper_usd": plan["budget"]["per_investigation_upper_usd"],
            "remaining_upper_usd": str(Decimal(plan["budget"]["calculated_upper_usd"]) - Decimal(plan["budget"]["per_investigation_upper_usd"])),
            "calculated_total_upper_usd": plan["budget"]["calculated_upper_usd"], "hard_cap_usd": plan["budget"]["total_upper_usd"]}
    plan["environment"]["EVIDENCE_RELEVANCE_PROTOCOL"] = args.protocol
    plan["environment"]["INVESTIGATION_PLANNER_PROTOCOL"] = args.business_contract
    if args.business_contract in {"native-v2", "native-v3"}:
        import sys
        sys.path.insert(0, str(ROOT / "backend"))
        from insight.investigation.business_contract_registry import VERSION, fingerprint, manifest
        plan["business_contract"] = {"version": VERSION, "registry_sha256": fingerprint(), "vocabulary": manifest(),
            "legacy_adaptation": "v1 remains v1; no label translation or rescoring"}
    # Previously stopped rounds and counters remain untouched. This binds new human authority.
    cap = plan["budget"]["total_upper_usd"]
    save(run / "plan.json", plan)
    save(run / "approval.json", {"approved": bool(args.authorization_reference), "transfer_confirmed": bool(args.authorization_reference),
        "plan_sha256": file_sha(run / "plan.json"), "max_total_usd": cap,
        "authorization_reference": args.authorization_reference,
        "processing_region": "DeepSeek default; geographic location unverified"})
    print(json.dumps({"run": str(run), "rows": [300, 30, 120, 48], "budget_usd": cap, "dispatches": 0}))


if __name__ == "__main__":
    main()
