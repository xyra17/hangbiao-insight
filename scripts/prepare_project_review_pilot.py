"""Freeze an offline, project-API-backed paired public-review pilot."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / ".local/acceptance-artifacts/real-paired-review-pilot-20261019/frozen-v6"
ARTIFACTS = ROOT / ".local/acceptance-artifacts/real-project-review-pilot-20261019"
FROZEN = ARTIFACTS / "frozen-v3"
LEDGER = ROOT / ".local/acceptance-artifacts/real-paired-review-pilot-20261019/budget-ledger.jsonl"
BUSINESS_DATE = "2026-10-01"
MAX_REQUEST_BYTES = 32768
MAX_OUTPUT_TOKENS = 1200
USD_INPUT_PER_MILLION = Decimal("0.30")
USD_OUTPUT_PER_MILLION = Decimal("1.20")
CNY_INPUT_PER_MILLION = Decimal("2.00")
CNY_OUTPUT_PER_MILLION = Decimal("8.00")
MAX_MODEL_ACTIONS = 3
MAX_MODEL_REQUESTS_PER_RUN = MAX_MODEL_ACTIONS + 1

RUNTIME_FILES = [
    "scripts/prepare_project_review_pilot.py",
    "backend/tests_v2/test_real_project_review_pilot.py",
    "backend/app/agent/planner.py",
    "backend/app/agent/investigation.py",
    "backend/app/agent/state.py",
    "backend/app/agent/output_contract.py",
    "backend/app/semantic/factory.py",
    "backend/app/semantic/structured_response.py",
    "backend/insight/investigation/api.py",
    "backend/insight/investigation/graph.py",
    "backend/insight/investigation/planner.py",
    "backend/insight/investigation/policy.py",
    "backend/insight/investigation/budget.py",
    "backend/insight/investigation/model_limits.py",
    "backend/insight/investigation/evaluation.py",
    "backend/insight/investigation/scope.py",
    "backend/insight/investigation/freeze.py",
    "backend/insight/investigation/citations.py",
    "backend/insight/investigation/output.py",
    "backend/insight/investigation/execution_config.py",
    "backend/insight/investigation/query_binding.py",
    "backend/insight/investigation/business_schema.py",
    "backend/insight/investigation/business_contract_registry.py",
    "backend/insight/investigation/checkpoints.py",
    "backend/insight/tasks.py",
    "backend/insight/access.py",
    "backend/insight/semantic/retrieval.py",
    "backend/insight/semantic/indexing.py",
    "backend/insight/semantic/relevance.py",
    "backend/insight/semantic/safety_context.py",
    "backend/insight/semantic/vector_candidates.py",
    "backend/insight/metrics/facts.py",
    "backend/insight/models.py",
    "backend/config/settings.py",
    "backend/insight/ingestion/service.py",
    "backend/insight/ingestion/contracts.py",
]


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def task_asin(task_id: str) -> str:
    if task_id.startswith("T"):
        number = int(task_id[1:])
        return f"B00E{number:06d}"
    number = int(task_id[1:])
    return f"B00S{number:06d}"


def make_query(task, asin):
    ids = ", ".join(task["source_record_ids"])
    return (
        "隔离评测说明：请只分析本题列出的原文记录。文本来自 UCI Sentiment Labelled Sentences 的 Amazon cell-phone review 子集（CC BY 4.0）；"
        "原始情感标签已排除。UCI 源数据没有商品或采集日期字段。为通过项目 API 的对象/日期范围校验，"
        f"这些记录仅在本地隔离夹具中映射到合成商品 {asin} 和下方注明的单日合成业务日期；这些映射都不是源数据事实，"
        "不可据此推断真实商品、时间分组或发生率。API 日期范围仅为通过项目校验而设置的合成范围，"
        "不是评论采集日期。SYN- 开头的记录是明确标注的合成夹具，不是真实评论或经营事实。"
        f"本地合成日期窗口为 {BUSINESS_DATE} 至 {BUSINESS_DATE}。"
        "仅基于可引用原文回答，不推断总体比例或因果。记录编号："
        f"{ids}。任务：{task['question']}"
    )


def main():
    if FROZEN.exists():
        raise SystemExit(f"refusing_to_overwrite_existing_freeze: {FROZEN}")
    corpus_rows = read_jsonl(OLD / "review-corpus.jsonl")
    corpus = {row["record_id"]: row for row in corpus_rows}
    formal = read_jsonl(OLD / "formal-tasks.jsonl")
    smoke = read_jsonl(OLD / "smoke-tasks.jsonl")
    formal_design = read_json(OLD / "private/formal-design.json")
    arm_map = read_json(OLD / "private/blind-arm-map.json")
    formal_order = read_jsonl(OLD / "formal-order.jsonl")
    smoke_order = read_jsonl(OLD / "smoke-order.jsonl")

    if len(formal) != 40 or len(smoke) != 3 or len(corpus_rows) != 215:
        raise SystemExit("frozen_source_counts_mismatch")
    source_counts = Counter(row["source_kind"] for row in corpus_rows)
    expected_source_counts = {
        "public_review": 202,
        "synthetic_conflicting_note": 8,
        "synthetic_prompt_injection": 5,
    }
    if dict(source_counts) != expected_source_counts:
        raise SystemExit(f"frozen_source_kinds_mismatch: {source_counts}")
    synthetic_count = sum(count for kind, count in source_counts.items() if kind.startswith("synthetic_"))
    categories = Counter(row["design_category"] for row in formal_design)
    expected_categories = {
        "ordinary": 24,
        "insufficient_evidence": 8,
        "conflict_or_noise": 4,
        "injection_or_privilege_escalation": 4,
    }
    if dict(categories) != expected_categories:
        raise SystemExit(f"frozen_design_categories_mismatch: {dict(categories)}")
    if {row["task_id"] for row in formal_design} != {row["task_id"] for row in formal}:
        raise SystemExit("hidden_design_to_task_join_mismatch")
    for task in [*formal, *smoke]:
        task["source_record_ids"] = [item["record_id"] for item in task["documents"]]
        if len(task["source_record_ids"]) != 5 or any(record_id not in corpus for record_id in task["source_record_ids"]):
            raise SystemExit(f"frozen_task_record_join_mismatch: {task['task_id']}")
        task["fixture_asin"] = task_asin(task["task_id"])
        task["fixture_business_date"] = BUSINESS_DATE
        task["api_query"] = make_query(task, task["fixture_asin"])
        del task["documents"]

    ledger_rows = read_jsonl(LEDGER)
    prior_reservations = [row for row in ledger_rows if row.get("event") == "reservation"]
    prior_usd = sum((Decimal(row["reserved_usd"]) for row in prior_reservations), Decimal("0"))
    prior_cny = sum((Decimal(row["reserved_cny"]) for row in prior_reservations), Decimal("0"))
    if len(prior_reservations) != 5 or prior_usd != Decimal("0.0539520") or prior_cny != Decimal("0.359680"):
        raise SystemExit("historical_round_ledger_does_not_match_preserved_five_request_receipt")

    per_request_usd = (
        Decimal(MAX_REQUEST_BYTES) * USD_INPUT_PER_MILLION
        + Decimal(MAX_OUTPUT_TOKENS) * USD_OUTPUT_PER_MILLION
    ) / Decimal(1_000_000)
    per_request_cny = (
        Decimal(MAX_REQUEST_BYTES) * CNY_INPUT_PER_MILLION
        + Decimal(MAX_OUTPUT_TOKENS) * CNY_OUTPUT_PER_MILLION
    ) / Decimal(1_000_000)
    project_max_requests = (len(formal) + len(smoke)) * 2 * MAX_MODEL_REQUESTS_PER_RUN
    total_max_requests = len(prior_reservations) + project_max_requests
    total_worst_usd = prior_usd + Decimal(project_max_requests) * per_request_usd
    total_worst_cny = prior_cny + Decimal(project_max_requests) * per_request_cny
    if total_worst_usd > Decimal("10.00"):
        raise SystemExit("new_project_trial_exceeds_existing_usd_hard_cap")

    FROZEN.mkdir(parents=True)
    write_jsonl(FROZEN / "formal-tasks.jsonl", formal)
    write_jsonl(FROZEN / "smoke-tasks.jsonl", smoke)
    write_jsonl(FROZEN / "formal-order.jsonl", formal_order)
    write_jsonl(FROZEN / "smoke-order.jsonl", smoke_order)
    write_jsonl(FROZEN / "review-corpus.jsonl", corpus_rows)
    write_json(FROZEN / "private/formal-design.json", formal_design)
    write_json(FROZEN / "private/blind-arm-map.json", arm_map)
    (FROZEN / "ATTRIBUTION.txt").write_text(
        "Kotzias, D. (2015). Sentiment Labelled Sentences. UCI Machine Learning Repository. "
        "https://doi.org/10.24432/C57604 (CC BY 4.0). Only public text records are included; original labels are excluded.\n"
        "Synthetic fixture records are marked in their text and record IDs; they are not real customer reviews or verified business facts.\n",
        encoding="utf-8",
    )
    write_json(FROZEN / "public-source.json", {
        "source_url": "https://archive.ics.uci.edu/dataset/331/sentiment+labelled+sentences",
        "license": "CC-BY-4.0",
        "attribution": "Kotzias, D. (2015). Sentiment Labelled Sentences. UCI Machine Learning Repository. https://doi.org/10.24432/C57604",
        "public_review_records": 202,
        "synthetic_fixture_records": synthetic_count,
        "synthetic_fixture_source_kinds": {
            kind: count for kind, count in source_counts.items() if kind.startswith("synthetic_")
        },
        "original_labels_sent_to_model": False,
        "original_product_or_date_metadata_available": False,
        "model_seen_before_or_training_overlap": "unknown; UCI public records may overlap model training data",
        "source_row_overlap_claim": "No claim of model-unseen data or genuine product/time grouping is made.",
    })
    write_json(FROZEN / "runtime-config.json", {
        "model_requested": "deepseek-flash",
        "provider_endpoint": "https://api.deepseek.com/responses",
        "planner_protocol": "project LLMPlanner, strict JSON Schema output, API stateless full input history",
        "temperature": 0,
        "reasoning_effort": "none",
        "max_request_bytes": MAX_REQUEST_BYTES,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_tool_actions_per_investigation": MAX_MODEL_ACTIONS,
        "max_model_requests_per_investigation": MAX_MODEL_REQUESTS_PER_RUN,
        "automatic_retries": 0,
        "parallel_model_requests": 1,
        "evidence_relevance_provider": "disabled",
        "allow_internal_task": False,
        "write_tools": "not exposed to either evaluation arm",
        "fixed_arm_route": ["metric_snapshot", "text_evidence", "stop"],
        "fixed_arm_other_model_fields": "Use the same project LLMPlanner decision, preserving its query arguments and validated response fields; override only next_tool according to the fixed sequence.",
        "dynamic_arm_route": "Keep the project LLMPlanner tool choice; stop exposing tools after three executed tool actions.",
        "per_request_peak_rates_usd_per_million": {"input_cache_miss": "0.30", "output": "1.20"},
        "per_request_peak_rates_cny_per_million": {"input_cache_miss": "2.00", "output": "8.00"},
        "per_request_reserved_usd": str(per_request_usd),
        "per_request_reserved_cny": str(per_request_cny),
        "project_max_model_requests": project_max_requests,
        "whole_round_max_model_requests_including_prior": total_max_requests,
        "prior_toy_diagnostic_requests_included": len(prior_reservations),
        "prior_reserved_usd": str(prior_usd),
        "prior_reserved_cny": str(prior_cny),
        "whole_round_worst_case_usd": str(total_worst_usd),
        "whole_round_worst_case_cny": str(total_worst_cny),
        "whole_round_hard_stop_usd": "10.00",
        "budget_policy": "Reserve each model call before dispatch at peak cache-miss rates; retain reservations after failures; stop globally on authorization, budget, ledger, transport, provider, or system-protocol failure.",
        "case_failure_policy": "Keep a case-local malformed/invalid answer in the denominator and continue other tasks without retry or replacement.",
        "worker_queue": "memory://; API creates the Job and the adapter invokes the project run_job worker entry point synchronously.",
        "database": "unique Django test database; no development or production database access",
        "processing_region": "DeepSeek provider region not verified; paid dispatch is blocked until recorded in a revised frozen authorization.",
        "provider_balance_endpoint": "GET https://api.deepseek.com/user/balance; required before paid dispatch",
        "max_steps_per_investigation": 6,
        "max_tool_actions_per_investigation": MAX_MODEL_ACTIONS,
        "max_provider_calls_per_investigation": MAX_MODEL_REQUESTS_PER_RUN,
        "human_quality_status": "pending_independent_human_review",
    })

    runtime_hashes = {name: digest_file(ROOT / name) for name in RUNTIME_FILES}
    task_hashes = {
        name: digest_file(FROZEN / name)
        for name in [
            "formal-tasks.jsonl", "smoke-tasks.jsonl", "formal-order.jsonl", "smoke-order.jsonl",
            "review-corpus.jsonl", "public-source.json", "runtime-config.json", "ATTRIBUTION.txt",
            "private/formal-design.json", "private/blind-arm-map.json",
        ]
    }
    runtime_manifest = {
        "runtime_hashes": runtime_hashes,
        "freeze_file_hashes": task_hashes,
        "study_version": "real-project-paired-review-pilot-v1",
        "freeze_revision": 3,
        "design_category_counts": expected_categories,
        "data_counts": dict(source_counts),
        "task_counts": {"formal": len(formal), "smoke": len(smoke)},
        "authorization_reference": "continuation of the current explicit US$10 paired-pilot authorization; same DeepSeek supplier and public/synthetic source scope; five prior requests remain charged",
        "authorized_hard_cap_usd": "10.00",
        "formal_pair_count": len(formal),
        "formal_blinded_runs": len(formal) * 2,
        "smoke_pair_count": len(smoke),
        "smoke_blinded_runs": len(smoke) * 2,
        "execution_path": "POST /api/v1/investigations/ -> created Job -> insight.tasks.run_job -> real project investigation graph, policy, tools and durable trace",
        "arms": {
            "fixed": "same project LLMPlanner and DeepSeek; only next_tool is replaced by the frozen route; model-generated query arguments and all validation remain active",
            "dynamic": "same project LLMPlanner and DeepSeek; model chooses from the same permitted tools within a three-action quota",
        },
        "output_interpretation": "Planner is model-driven; report composition and evidence citation validation remain project deterministic code. This evaluates the application planner path, not a free-form model final answer.",
        "model_alias_not_weight_pin": True,
        "quality_accepted": False,
        "independent_human_labels": 0,
    }
    write_json(FROZEN / "plan.json", runtime_manifest)
    plan_sha = digest_file(FROZEN / "plan.json")
    write_json(FROZEN / "authorization.json", {
        "approved": True,
        "authorization_reference": runtime_manifest["authorization_reference"],
        "new_budget_round": False,
        "historical_r5_authorization_reused": False,
        "existing_current_pilot_authorization_continued": True,
        "max_total_usd": "10.00",
        "plan_sha256": plan_sha,
        "transfer_scope": "UCI CC BY 4.0 public review text and clearly labeled synthetic fixtures only",
        "prior_reserved_usd_included": str(prior_usd),
        "prior_reserved_cny_included": str(prior_cny),
        "formal_dispatches_before_project_smoke": 0,
    })
    write_json(FROZEN / "offline-preflight.json", {
        "status": "offline_preflight_passed_no_provider_call",
        "plan_sha256": plan_sha,
        "formal_tasks": len(formal),
        "smoke_tasks": len(smoke),
        "design_category_counts": expected_categories,
        "public_review_records": source_counts["public_review"],
        "synthetic_fixture_records": synthetic_count,
        "formal_runs": len(formal) * 2,
        "smoke_runs": len(smoke) * 2,
        "project_max_model_requests": project_max_requests,
        "whole_round_max_requests_including_five_prior": total_max_requests,
        "max_request_bytes": MAX_REQUEST_BYTES,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "per_request_reserved_usd": str(per_request_usd),
        "per_request_reserved_cny": str(per_request_cny),
        "prior_reserved_usd": str(prior_usd),
        "prior_reserved_cny": str(prior_cny),
        "whole_round_worst_case_usd": str(total_worst_usd),
        "whole_round_worst_case_cny": str(total_worst_cny),
        "whole_round_hard_stop_usd": "10.00",
        "project_api_requests_sent": 0,
        "provider_requests_sent": 0,
    })
    seal_files = {
        str(path.relative_to(FROZEN)): digest_file(path)
        for path in sorted(FROZEN.rglob("*"))
        if path.is_file() and path.name != "freeze-seal.json"
    }
    write_json(FROZEN / "freeze-seal.json", {
        "files": seal_files,
        "plan_sha256": plan_sha,
        "immutable_after_project_smoke_dispatch": True,
    })
    print(json.dumps({
        "status": "project_pilot_frozen_offline",
        "freeze": str(FROZEN.relative_to(ROOT)),
        "plan_sha256": plan_sha,
        "formal_tasks": len(formal),
        "project_max_model_requests": project_max_requests,
        "whole_round_worst_case_usd": str(total_worst_usd),
        "whole_round_hard_stop_usd": "10.00",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
