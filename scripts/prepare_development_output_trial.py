"""Freeze 10 truncated + 10 completed development cases without labels."""

import argparse
import hashlib
import json
import shutil
import uuid
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-run", type=Path, required=True)
    p.add_argument("--input", type=Path, required=True)
    a = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = a.source_run.resolve()
    if (
        sha(a.input)
        != "4622f946357cc4595155f5020340ef3832d09c077ab00bbb9d2e150a3a912396"
    ):
        raise ValueError("frozen input required")
    rows = [json.loads(s) for s in a.input.read_text().splitlines()]
    rank = lambda row: hashlib.sha256(
        ("development-no-thinking-v1|" + row["review_id"]).encode()
    ).hexdigest()
    groups = {"truncated": [], "completed": []}
    source_hashes = []
    for index, row in enumerate(rows, 1):
        if row["split"] != "development":
            continue
        path = source / "receipts" / f"{index:04}.json"
        receipt = json.loads(path.read_text())
        if receipt["review_id"] != row["review_id"]:
            raise ValueError("source receipt identity mismatch")
        response = receipt.get("response", {})
        status = response.get("status")
        reason = (response.get("incomplete_details") or {}).get("reason")
        source_hashes.append({"path": str(path), "sha256": sha(path)})
        stratum = (
            "truncated"
            if status == "incomplete" and reason == "max_output_tokens"
            else "completed"
            if status == "completed"
            else None
        )
        if stratum:
            groups[stratum].append(
                {"row": row, "rank": rank(row), "receipt": receipt, "path": path}
            )
    chosen = []
    for stratum in ["truncated", "completed"]:
        group = sorted(groups[stratum], key=lambda v: v["rank"])
        if len(group) < 10:
            raise ValueError("insufficient development stratum")
        chosen.extend((stratum, entry) for entry in group[:10])
    run = (
        root
        / ".local/acceptance-artifacts"
        / ("development-no-thinking-20261011-" + uuid.uuid4().hex[:12])
    )
    run.mkdir()
    (run / "snapshot/backend/app/semantic").mkdir(parents=True)
    (run / "snapshot/backend/app/__init__.py").write_text("")
    for path in (root / "backend/app/semantic").glob("*.py"):
        shutil.copyfile(path, run / "snapshot/backend/app/semantic" / path.name)
    shutil.copyfile(
        root / "dataset/data/03_黄金评论集/主题分类体系_v1_1.json",
        run / "taxonomy.json",
    )
    for name in [
        "prepare_development_output_trial.py",
        "run_development_output_trial.py",
        "summarize_development_output_trial.py",
    ]:
        shutil.copyfile(root / "scripts" / name, run / name)
    (run / "prediction-input.jsonl").write_text(
        "".join(
            json.dumps(
                {"review_id": v["row"]["review_id"], "text": v["row"]["text"]},
                ensure_ascii=False,
            )
            + "\n"
            for _, v in chosen
        )
    )
    baseline = []
    for stratum, v in chosen:
        response = v["receipt"]["response"]
        usage = response["usage"]
        request = v["receipt"]["request"]
        if (
            request["reasoning"] != {"effort": "low"}
            or request["max_output_tokens"] != 1200
        ):
            raise ValueError("unexpected baseline config")
        baseline.append(
            {
                "review_id": v["row"]["review_id"],
                "split": "development",
                "stratum": stratum,
                "sampling_rank": v["rank"],
                "source_receipt": str(v["path"]),
                "source_receipt_sha256": sha(v["path"]),
                "baseline_response_status": response["status"],
                "baseline_usage": usage,
                "baseline_latency_ms": v["receipt"]["latency_ms"],
                "baseline_request_sha256": hashlib.sha256(
                    json.dumps(request, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest(),
            }
        )
    (run / "sampling-baseline.json").write_text(
        json.dumps(
            {
                "rule": "sha256(development-no-thinking-v1|review_id), ascending independently per stratum; first 10 truncated + first 10 completed; no labels",
                "development_pool": {k: len(v) for k, v in groups.items()},
                "source_receipts": source_hashes,
                "sample": baseline,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    refs = [
        {"path": str(path.relative_to(run)), "sha256": sha(path)}
        for path in sorted(run.rglob("*"))
        if path.is_file()
    ]
    plan = {
        "status": "prepared_not_approved_not_run",
        "run_id": run.name,
        "repository": str(root),
        "source_run": str(source),
        "source_input_sha256": sha(a.input),
        "runtime_references": refs,
        "split": "development",
        "rows": 20,
        "strata": {"truncated": 10, "completed": 10},
        "candidate_changes": {"reasoning.effort": ["low", "none"]},
        "model": "deepseek-flash",
        "prompt_version": "semantic-prompt-v1.2",
        "taxonomy_version": "taxonomy-v1.1",
        "max_output_tokens": 1200,
        "max_request_bytes": 8000,
        "max_calls": 20,
        "max_attempts": 1,
        "price_ceiling_usd_per_million": {"input": "0.30", "output": "1.20"},
        "per_call_upper_usd": "0.00384",
        "total_upper_usd": "0.0768",
        "requested_total_hard_cap_usd": "0.08",
        "requested_call_hard_cap_usd": "0.004",
        "completion_criteria": {
            "provider_completed_nonempty": "20/20",
            "strict_parser_success": "20/20",
            "rejected_claims": 0,
            "reported_reasoning_tokens": 0,
            "reasoning_usage_unknown_rows": 0,
            "reported_model": "deepseek-flash for all 20 responses",
            "budget_and_payload_bounds": "all pass",
        },
        "mandatory_reporting": [
            "stratum completeness and strict parse rates",
            "accepted/rejected/empty-claim counts",
            "remaining failure codes",
            "input/cache/output/reasoning tokens",
            "known usage price estimate and unknown reservation",
            "latency per stratum",
        ],
        "limit": "one candidate one 20-call trial; no expansion/retries; no validation or holdout calls; no semantic accuracy or full-data claim",
        "official_parameters": {
            "url": "https://api-docs.deepseek.com/api/create-response/",
            "thinking_guide": "https://api-docs.deepseek.com/guides/thinking_mode/",
            "checked_on": "2026-10-11",
            "supported_none_disables_thinking": True,
            "max_output_includes_reasoning": True,
        },
        "transfer": "same supplier, existing 20 public development texts, unchanged product prompt/taxonomy/schema, constant runtime IDs; no source IDs/splits/labels/business data sent",
    }
    (run / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    )
    (run / "approval-template.json").write_text(
        json.dumps(
            {
                "approved": False,
                "plan_sha256": sha(run / "plan.json"),
                "max_total_usd": "0.08",
                "max_call_usd": "0.004",
                "transfer_confirmed": False,
            },
            indent=2,
        )
        + "\n"
    )
    print(run)


if __name__ == "__main__":
    main()
