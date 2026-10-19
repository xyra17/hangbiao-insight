"""Freeze, execute, seal and separately score one bounded quality candidate."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from quality_round_guard import file_sha, save  # noqa: E402


def prepare(run, reviews, embedding_config):
    if run.exists() or not run.resolve().is_relative_to(ROOT / ".local/acceptance-artifacts"):
        raise ValueError("unique acceptance directory required")
    if file_sha(reviews) != "4622f946357cc4595155f5020340ef3832d09c077ab00bbb9d2e150a3a912396":
        raise ValueError("existing frozen 300-review input required")
    rows = [json.loads(line) for line in reviews.read_text().splitlines()]
    if len(rows) != 300:
        raise ValueError("300 rows required; no relabeling or expansion")
    embedding = json.loads(embedding_config.read_text())
    run.mkdir(parents=True)
    inputs = run / "inputs"
    inputs.mkdir()
    corpus = ROOT / "dataset/data/07_S4_S6独立评测_v1/runtime_corpus_v4"
    for name in ("corpus_manifest.json", "support_cases.csv", "retrieval_runtime_queries.csv", "paired_runtime_inputs.csv"):
        shutil.copyfile(corpus / name, inputs / name)
    (inputs / "prediction-input.jsonl").write_text("".join(json.dumps({"review_id": row["review_id"], "text": row["text"]}, ensure_ascii=False) + "\n" for row in rows))
    shutil.copyfile(ROOT / "dataset/data/03_黄金评论集/主题分类体系_v1_1.json", run / "taxonomy.json")
    files = [p for directory in ("backend/app", "backend/insight", "backend/config") for p in (ROOT / directory).rglob("*.py")]
    files += [ROOT / "backend" / name for name in ("pyproject.toml", "uv.lock", "pytest-v2.ini", "Dockerfile.v2")]
    files += [ROOT / "backend/tests_v2" / name for name in ("conftest.py", "isolation_tasks.py", "test_s4_s6_runtime_acceptance.py", "test_unified_quality_round.py")]
    files += [ROOT / "scripts" / name for name in ("quality_round_guard.py", "unified_quality_acceptance.py", "score_public_product_agent.py")]
    files += [ROOT / "dataset" / name for name in ("adapt_investigation_outputs.py", "score_paired_investigations.py",
             "quality_gate.py", "evaluation_bundle.py", "audit_investigation_capabilities.py")]
    hashes = {}
    for path in files:
        destination = run / "snapshot" / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        hashes[str(destination.relative_to(run))] = file_sha(destination)
    for path in inputs.iterdir():
        hashes[str(path.relative_to(run))] = file_sha(path)
    hashes["taxonomy.json"] = file_sha(run / "taxonomy.json")
    calls = {}
    for kind, request_bytes, output_tokens in (("planner", 24000, 3000), ("relevance", 24000, 6000), ("classification", 8000, 1200)):
        # Relevance reserves request bytes + 1024 internally; retain that overhead.
        input_bound = request_bytes + (1024 if kind == "relevance" else 0)
        calls[kind] = {"request_bytes": request_bytes, "output_tokens": output_tokens,
                       "upper_usd": str((Decimal(input_bound) * Decimal(".30") + Decimal(output_tokens) * Decimal("1.20")) / 1000000)}
    per_run = 12 * max(Decimal(calls[k]["upper_usd"]) for k in ("planner", "relevance"))
    total = 48 * per_run + 30 * Decimal(calls["relevance"]["upper_usd"]) + 300 * Decimal(calls["classification"]["upper_usd"])
    cap = total.quantize(Decimal(".01"), rounding=ROUND_CEILING)
    environment = {"INVESTIGATION_PLANNER_PROVIDER": "deepseek", "INVESTIGATION_EVAL_PROVIDER": "deepseek",
        "INSIGHT_ENABLED_MODEL_PROVIDERS": "deepseek", "INVESTIGATION_MODEL": "deepseek-flash", "EVIDENCE_RELEVANCE_MODEL": "deepseek-flash",
        "INVESTIGATION_REASONING_EFFORT": "none", "EVIDENCE_RELEVANCE_REASONING_EFFORT": "none",
        "INVESTIGATION_MAX_REQUEST_BYTES": "24000", "INVESTIGATION_MAX_OUTPUT_TOKENS": "3000",
        "INVESTIGATION_MAX_CALL_COST": calls["planner"]["upper_usd"], "INVESTIGATION_MAX_RUN_COST": str(per_run),
        "INVESTIGATION_MAX_EVAL_COST": str(cap), "INVESTIGATION_COST_CURRENCY": "USD",
        "INVESTIGATION_INPUT_COST_PER_MILLION": ".30", "INVESTIGATION_OUTPUT_COST_PER_MILLION": "1.20",
        "EVIDENCE_RELEVANCE_ENABLED": "1", "EVIDENCE_RELEVANCE_PROVIDER": "deepseek", "EVIDENCE_RELEVANCE_MAX_CANDIDATES": "20",
        "EVIDENCE_RELEVANCE_MAX_REQUEST_BYTES": "24000", "EVIDENCE_RELEVANCE_MAX_OUTPUT_TOKENS": "6000",
        "EVIDENCE_RELEVANCE_MAX_COST": calls["relevance"]["upper_usd"], "EVIDENCE_CANDIDATE_MODE": "hybrid_local",
        "EVIDENCE_LOCAL_EMBEDDING_MODEL": embedding["model"], "EVIDENCE_LOCAL_EMBEDDING_SHA256": embedding["model_sha256"],
        "EVIDENCE_LOCAL_EMBEDDING_URL": embedding["url"], "INVESTIGATION_REGION": "approval-required",
        "EVIDENCE_RELEVANCE_REGION": "approval-required", "INVESTIGATION_APPROVAL_REFERENCE": "offline-preparation-only",
        "EVIDENCE_RELEVANCE_APPROVAL_REFERENCE": "offline-preparation-only"}
    plan = {"version": "unified-quality-round-v1", "status": "prepared_not_approved", "repository": str(ROOT),
        "model": "deepseek-flash", "model_version_pinned": False, "returned_model_receipts_required": True,
        "as_of": "2026-09-30T12:00:00+00:00", "runtime_hashes": hashes, "environment": environment,
        "budget": {"calls": calls, "investigation_call_units": 12, "per_investigation_upper_usd": str(per_run),
                   "max_calls": 300 + 30 + 48 * 12, "calculated_upper_usd": str(total), "total_upper_usd": str(cap),
                   "input_usd_per_million": ".30", "output_usd_per_million": "1.20", "max_retries": 0,
                   "stop_on_provider_or_contract_failure": True, "unknown_usage_reservation_retained": True,
                   "past_approved_balance_reused": False},
        "input_sha256": file_sha(reviews), "reviews_source": str(reviews.resolve()),
        "classification": {"rows": 300, "prompt": "semantic-prompt-v1.2", "taxonomy": "taxonomy-v1.1", "mapping": "product-to-public-broad-v1",
                           "purpose": "same frozen silver development agreement; taxonomy mismatch and absent appearance/service remain unsupported"},
        "scope": "30 queries/120 retrieval docs, 48 investigation traces, 300 existing semantic inputs; seen regression, not new blind acceptance",
        "not_covered_by_silver": ["independent intent accuracy", "independent semantic accuracy", "severe safety and unsafe-output adjudication", "rare topics", "business pilot"],
        "independent_investigation_fields": 384, "independent_adjudications": 0, "production_publish_approved": False,
        "price_source": "https://api-docs.deepseek.com/quick_start/pricing/"}
    save(run / "plan.json", plan)
    save(run / "approval.json", {"approved": False, "transfer_confirmed": False, "plan_sha256": file_sha(run / "plan.json"),
        "max_total_usd": str(cap), "authorization_reference": None, "processing_region": None})
    return {"run": str(run), "whole_round_upper_usd": str(cap), "max_calls": plan["budget"]["max_calls"]}


def execute(run, paid):
    plan = json.loads((run / "plan.json").read_text())
    environment = {**os.environ, **plan["environment"], "QUALITY_UNIFIED_RUN": str(run), "QUALITY_UNIFIED_PAID": "1" if paid else "0"}
    real_reviews = plan.get("version") == "real-review-quality-round-v1"
    if real_reviews:
        environment.pop("QUALITY_UNIFIED_RUN", None)
        environment.update(QUALITY_REAL_REVIEW_RUN=str(run), QUALITY_REAL_REVIEW_PAID="1" if paid else "0")
        environment["PGDATABASE"] = "insight_acceptance_20261013_v" + str(int(file_sha(run / "plan.json")[:6], 16) + 1000)
    if paid:
        approval = json.loads((run / "approval.json").read_text())
        if not approval.get("approved") or not approval.get("processing_region"):
            raise ValueError("fresh whole-round approval required")
        for prefix in ("INVESTIGATION", "EVIDENCE_RELEVANCE"):
            environment[prefix + "_REGION"] = approval["processing_region"]
            environment[prefix + "_APPROVAL_REFERENCE"] = approval["authorization_reference"]
    snapshot = run / "snapshot/backend"
    environment["PYTHONPATH"] = str(snapshot) + os.pathsep + str(run / "snapshot/scripts")
    command = [str(ROOT / "backend/.venv/bin/pytest"), "-c", str(snapshot / "pytest-v2.ini"),
               str(snapshot / "tests_v2" / ("test_real_review_quality_round.py" if real_reviews else "test_unified_quality_round.py")), "-q"]
    with (run / ("paid-execution.log" if paid else "preflight.log")).open("x") as stream:
        result = subprocess.run(command, cwd=snapshot, env=environment, stdout=stream, stderr=subprocess.STDOUT, check=False)
    return result.returncode


def validate_full_runtime_coverage(directory):
    runtime = json.loads((directory / "runtime.json").read_text())
    retrieval = json.loads((directory / "retrieval-runtime.json").read_text())
    classification = json.loads((directory / "raw-predictions.json").read_text())
    coverage = {"investigation": len(runtime["trajectories"]), "retrieval": len(retrieval["per_query"]),
                "classification": len(classification["predictions"])}
    if coverage != {"investigation": 48, "retrieval": 30, "classification": 300}:
        raise ValueError("partial runtime cannot receive full-coverage scoring: " + json.dumps(coverage, sort_keys=True))
    return runtime, retrieval


def score(run, mode, adjudications=None, silver=None, issues=None):
    plan = json.loads((run / "plan.json").read_text())
    if plan.get("version") == "real-review-quality-round-v1":
        raise ValueError("use scripts/score_real_review_quality.py with the plan-bound real-review adjudication pack; historical qrels and AI silver are not valid for this corpus")
    for relative, digest in plan["runtime_hashes"].items():
        if file_sha(run / relative) != digest:
            raise ValueError("frozen_scoring_or_candidate_mismatch")
    sys.path.insert(0, str(run / "snapshot/backend"))
    sys.path.insert(0, str(run / "snapshot/scripts"))
    directory = run / mode
    for name, digest in json.loads((directory / "seal.json").read_text()).items():
        if file_sha(directory / name) != digest:
            raise ValueError("runtime seal mismatch")
    # Reject incomplete runs before creating full-scoring artifacts or reading qrels.
    runtime, retrieval = validate_full_runtime_coverage(directory)
    output = run / (mode + "-evaluation")
    output.mkdir()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from insight.investigation.evaluation import adapt_prediction, write_template
    from insight.investigation.execution_config import observed_text_parameters
    from tests_v2 import test_s4_s6_runtime_acceptance as helpers
    helpers.RUNTIME = run / "inputs"
    qrels = helpers.read_csv(ROOT / "dataset/data/07_S4_S6独立评测_v1/retrieval_qrels.csv")
    summary = helpers.summarize_retrieval(retrieval["queries"], retrieval["per_query"], qrels,
        date.fromisoformat(retrieval["date_scope"]["start"]), date.fromisoformat(retrieval["date_scope"]["end"]))
    summary.update(per_query=retrieval["per_query"], real_provider_response=retrieval["real_provider_response"], frozen_test_is_seen_regression=True)
    save(output / "retrieval.json", summary)
    traces = runtime["trajectories"]
    gold = helpers.read_csv(ROOT / "dataset/data/07_S4_S6独立评测_v1/paired_investigation_scenarios.csv")
    groups = {}
    by_id = {trace["scenario_id"]: trace for trace in traces}
    for row in gold:
        groups.setdefault(row["pair_id"], []).append(by_id[row["scenario_id"]])
    differences = []
    for pair_id, pair in groups.items():
        names = [tuple(tool["name"] for tool in trace["tools"] if tool["status"] == "completed") for trace in pair]
        params = [observed_text_parameters(trace) for trace in pair]
        post = [observed_text_parameters(trace, after_evidence=True) for trace in pair]
        records = [{key for request in trace["retrievals"] for key in request["record_ids"]} for trace in pair]
        changed = all(post) and post[0] != post[1]
        evidence = records[0] != records[1]
        differences.append({"pair_id": pair_id, "tool_names_changed": names[0] != names[1],
            "executed_search_parameter_changed": params[0] != params[1], "post_evidence_executed_parameter_changed": changed,
            "follow_up_query_changed_after_evidence": changed, "retrieved_evidence_changed": evidence,
            "follow_up_parameter_changed_with_evidence": changed and evidence,
            "meaningful_semantic_difference": "pending_independent_review"})
    predictions = [adapt_prediction(trace)[0] for trace in traces]
    runtime.update(pair_differences=differences, predictions=predictions,
        pairs_with_tool_or_parameter_divergence=sum(d["tool_names_changed"] or d["follow_up_query_changed_after_evidence"] for d in differences),
        pairs_with_evidence_driven_follow_up_parameter_divergence=sum(d["follow_up_parameter_changed_with_evidence"] for d in differences),
        run_terminal_success_count=sum(t["status"] in {"completed", "needs_clarification"} for t in traces))
    save(output / "runtime.json", runtime)
    write_template(output / "adjudications.csv", traces)
    save(output / "adjudication-status.json", {"trace_count": 48, "field_count": 384, "independent_adjudicated_fields": 0,
        "rule_or_model_proposals_are_not_adjudications": True, "trace_hash_binding": True,
        "minimum_inputs": ["independent reviewer identity", "zoned timestamp", "eight semantic judgments for each bound trace", "explicit unknown for unsupported topics"]})
    if silver and issues:
        import score_public_product_agent
        original_rows = [json.loads(line) for line in Path(json.loads((run / "plan.json").read_text())["reviews_source"]).read_text().splitlines()]
        classification = score_public_product_agent.score(original_rows, json.loads(silver.read_text())["labels"],
            json.loads((directory / "raw-predictions.json").read_text())["predictions"], json.loads(issues.read_text()))
        classification["bindings"] = {"runtime_seal_sha256": file_sha(directory / "seal.json"),
            "silver_sha256": file_sha(silver), "issues_sha256": file_sha(issues), "mapping_script_sha256": file_sha(run / "snapshot/scripts/score_public_product_agent.py")}
        classification["independent_accuracy_certified"] = False
        save(output / "classification-silver-agreement.json", classification)
    commands = [("adapt_investigation_outputs.py", ["--runtime-report", output / "runtime.json", "--output", output / "adapted.json"] +
                (["--adjudications", adjudications] if adjudications else [])),
        ("score_paired_investigations.py", ["--runtime-report", output / "runtime.json", "--adapted", output / "adapted.json", "--pack-dir", ROOT / "dataset/data/07_S4_S6独立评测_v1", "--output", output / "scoring.json"]),
        ("quality_gate.py", ["--retrieval", output / "retrieval.json", "--paired", output / "runtime.json", "--adapted", output / "adapted.json",
            "--scoring", output / "scoring.json", "--bundle-output", output / "bundle.json", "--output", output / "gate.json"]),
        ("audit_investigation_capabilities.py", ["--runtime-report", output / "bundle.json", "--output", output / "capabilities.json"])]
    outcomes = []
    for name, flags in commands:
        result = subprocess.run([sys.executable, str(run / "snapshot/dataset" / name), *map(str, flags)], capture_output=True, text=True, check=False)
        (output / (name + ".log")).write_text(result.stdout + result.stderr)
        outcomes.append({"command": name, "exit": result.returncode})
        expected_artifact = {"adapt_investigation_outputs.py": "adapted.json", "score_paired_investigations.py": "scoring.json",
                             "quality_gate.py": "gate.json", "audit_investigation_capabilities.py": "capabilities.json"}[name]
        allowed = {0, 1} if name in {"score_paired_investigations.py", "quality_gate.py"} else {0}
        if result.returncode not in allowed or not (output / expected_artifact).is_file():
            raise ValueError("post-seal scoring chain failed: " + name)
    save(output / "chain-result.json", {"commands": outcomes, "offline_substitution": mode == "offline", "quality_accepted": False,
        "classification_agreement": "separate_frozen_mapping_scoring_required", "independent_intent_safety": "not_run"})
    return outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "preflight", "run", "score"])
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--embedding-config", type=Path)
    parser.add_argument("--mode", choices=["offline", "paid"], default="paid")
    parser.add_argument("--adjudications", type=Path)
    parser.add_argument("--silver", type=Path)
    parser.add_argument("--issues", type=Path)
    args = parser.parse_args()
    run = args.run.resolve()
    if args.action == "prepare":
        print(json.dumps(prepare(run, args.reviews, args.embedding_config)))
    elif args.action in {"preflight", "run"}:
        return execute(run, args.action == "run")
    else:
        print(json.dumps(score(run, args.mode, args.adjudications, args.silver, args.issues)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
