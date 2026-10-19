"""Prepare a unique frozen product semantic run without reading silver labels."""

import argparse
import hashlib
import json
import shutil
import uuid
from pathlib import Path


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if (
        sha(args.input)
        != "4622f946357cc4595155f5020340ef3832d09c077ab00bbb9d2e150a3a912396"
        or sha(args.manifest)
        != "1f338a5cfc8593c30d8021a57235e8786b8ab0d6005357e81232583c31fa1469"
    ):
        raise ValueError("frozen input/manifest required")
    rows = [json.loads(s) for s in args.input.read_text().splitlines()]
    if len(rows) != 300 or len({r["review_id"] for r in rows}) != 300:
        raise ValueError("frozen 300 unique reviews required")
    run = (
        root
        / ".local/acceptance-artifacts"
        / ("public-product-agent-20261010-" + uuid.uuid4().hex[:12])
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
        "public_agent_prediction.py",
        "score_public_product_agent.py",
        "prepare_public_product_agent.py",
    ]:
        shutil.copyfile(root / "scripts" / name, run / name)
    (run / "prediction-input.jsonl").write_text(
        "".join(
            json.dumps(
                {"review_id": r["review_id"], "text": r["text"]}, ensure_ascii=False
            )
            + "\n"
            for r in rows
        )
    )
    refs = [
        {"path": str(p.relative_to(run)), "sha256": sha(p)}
        for p in sorted(run.rglob("*"))
        if p.is_file()
    ]
    plan = {
        "status": "prepared_not_approved_not_executed",
        "repository": str(root),
        "run_id": run.name,
        "input_sha256": sha(args.input),
        "manifest_sha256": sha(args.manifest),
        "runtime_references": refs,
        "rows": 300,
        "splits": {
            s: sum(r["split"] == s for r in rows)
            for s in ["development", "validation", "holdout"]
        },
        "entrypoint": "app.semantic.factory.build_semantic_service -> SemanticExtractionService.extract -> production PromptBuilder/Parser/Guard",
        "prompt_version": "semantic-prompt-v1.2",
        "taxonomy_version": "taxonomy-v1.1",
        "model": "deepseek-flash",
        "max_calls": 300,
        "max_attempts": 1,
        "max_request_bytes": 8000,
        "max_output_tokens": 1200,
        "price_ceiling_usd_per_million": {"input": "0.30", "output": "1.20"},
        "price_source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "per_call_conservative_upper_usd": "0.00384",
        "total_conservative_upper_usd": "1.152",
        "requested_total_hard_cap_usd": "1.20",
        "requested_call_hard_cap_usd": "0.004",
        "transfer_fields": [
            "text",
            "constant runtime-review/runtime-product IDs",
            "null rating/date",
            "empty title",
            "product prompt/schema/taxonomy",
        ],
        "source_ids_splits_labels_transferred": False,
        "version_pinned": False,
        "label_isolation": "worker Python audit denies repository reads except prepared run and Python venv; safe text/id input only; labels scored after prediction seal",
        "scope": "existing product semantic chain with post-prediction broad-topic mapping; no investigation/planner/retrieval run",
        "mapping_version": "product-to-public-broad-v1",
    }
    (run / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    )
    print(run)


if __name__ == "__main__":
    main()
