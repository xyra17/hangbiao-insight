"""Free post-seal development agreement audit; never dispatches a model request."""

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

from score_public_product_agent import metrics, project


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, required=True)
    parser.add_argument("--silver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trial = args.trial.resolve()
    seal = json.loads((trial / "prediction-seal.json").read_text())
    for name, field in [
        ("plan.json", "plan_sha256"),
        ("raw-results.json", "results_sha256"),
        ("execution-report.json", "execution_report_sha256"),
    ]:
        if sha(trial / name) != seal[field]:
            raise ValueError("prediction seal mismatch")
    sample = json.loads((trial / "sampling-baseline.json").read_text())["sample"]
    plan = json.loads((trial / "plan.json").read_text())
    for ref in plan["runtime_references"]:
        if sha(trial / ref["path"]) != ref["sha256"]:
            raise ValueError("sealed runtime reference mismatch")
    inputs = {
        r["review_id"]: r
        for r in map(json.loads, (trial / "prediction-input.jsonl").read_text().splitlines())
    }
    predictions = {r["review_id"]: r for r in json.loads((trial / "raw-results.json").read_text())["results"]}
    if (
        len(sample) != 20
        or set(inputs) != set(predictions)
        or set(inputs) != {s["review_id"] for s in sample}
    ):
        raise ValueError("sample identity mismatch")
    # Only selected development labels enter scoring; no validation/holdout tuning.
    labels = {
        r["review_id"]: r for r in json.loads(args.silver.read_text())["labels"] if r["review_id"] in inputs
    }
    sys.path.insert(0, str(trial / "snapshot/backend"))
    from app.semantic.guard import SemanticGuard
    from app.semantic.parser import SemanticParser
    from app.semantic.schemas import GatewayResponse, ReviewInput
    from app.semantic.taxonomy import TaxonomyLoader

    guard = SemanticGuard(TaxonomyLoader.load(trial / "taxonomy.json"))
    items, rows, old_completed = [], [], []
    for entry in sample:
        key = entry["review_id"]
        label = labels[key]
        if (
            entry["split"] != "development"
            or label["split"] != "development"
            or label["text"] != inputs[key]["text"]
        ):
            raise ValueError("development input binding mismatch")
        new = project(predictions[key]["guarded"])
        if predictions[key]["status"] != "parsed":
            raise ValueError("trial row did not complete strict parsing")
        receipt_path = Path(entry["source_receipt"])
        if sha(receipt_path) != entry["source_receipt_sha256"]:
            raise ValueError("baseline receipt mismatch")
        receipt = json.loads(receipt_path.read_text())
        if receipt["review_id"] != key:
            raise ValueError("baseline identity mismatch")
        old = {"status": "failed", "polarity": None, "topics": []}
        if entry["stratum"] == "completed":
            response = receipt["response"]
            payload = "".join(
                c["text"]
                for o in response["output"]
                if o.get("type") == "message"
                for c in o.get("content", [])
                if c.get("type") == "output_text"
            )
            review = ReviewInput(
                review_id="runtime-review", product_id="runtime-product", text=inputs[key]["text"]
            )
            parsed = SemanticParser().parse(
                review,
                GatewayResponse(
                    payload=payload,
                    provider="deepseek",
                    api_name="responses",
                    model_name=receipt["reported_model"],
                    reasoning_effort="low",
                    prompt_version="semantic-prompt-v1.2",
                    taxonomy_version="taxonomy-v1.1",
                ),
            )
            old = project(guard.apply(review, parsed).model_dump(mode="json"))
            old_completed.append({"gold": label["label"], "prediction": old})
        items.append({"gold": label["label"], "prediction": new, "stratum": entry["stratum"]})
        rows.append(
            {
                "review_id": key,
                "stratum": entry["stratum"],
                "text": inputs[key]["text"],
                "silver": label["label"],
                "old": old,
                "new": new,
                "changed": old != new,
            }
        )
    result = {
        "status": "AI_silver_agreement_only",
        "accuracy_claim": False,
        "new_blind_test": False,
        "bindings": {
            "prediction_seal_sha256": sha(trial / "prediction-seal.json"),
            "silver_sha256": sha(args.silver),
        },
        "all_rows": metrics(items),
        "strata": {s: metrics([i for i in items if i["stratum"] == s]) for s in ["truncated", "completed"]},
        "old_completed": metrics(old_completed),
        "completed_changed": sum(r["changed"] for r in rows if r["stratum"] == "completed"),
        "rows": rows,
        "limitations": [
            "Same-provider silver labels are not independent truth.",
            "20 selected development rows do not establish full quality acceptance.",
            "Fixed charger taxonomy has no appearance/service projection.",
            "Retrieval and investigation gates remain separate.",
        ],
    }
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "agreement.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    with (args.output / "paired.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(
            {k: json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v for k, v in r.items()}
            for r in rows
        )
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
