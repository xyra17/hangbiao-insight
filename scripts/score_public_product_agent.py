"""Post-seal product-taxonomy projection and AI silver agreement scoring."""

import argparse
import hashlib
import json
from pathlib import Path

TOPICS = [
    "function",
    "durability",
    "compatibility",
    "appearance",
    "value",
    "service",
    "other",
]
PRIMARY = {
    "function_performance": "function",
    "reliability_durability": "durability",
    "compatibility_fit": "compatibility",
    "safety_risk": "other",
    "usability_installation": "function",
    "product_quality": "other",
}


def project(guarded):
    claims = [c for c in guarded["predictions"] if c["prediction_status"] == "accepted"]
    if not claims:
        return {"status": "abstain", "polarity": None, "topics": []}
    topics, polarities = set(), set()
    for claim in claims:
        primary, secondary = claim["primary_topic"], claim["secondary_topic"]
        if primary == "other_unknown":
            return {"status": "uncertain", "polarity": None, "topics": []}
        if primary == "convenience_value":
            topic = "value" if secondary == "price_value" else "function"
        else:
            topic = PRIMARY[primary]
        topics.add(topic)
        polarities.add(claim["polarity"])
    if polarities == {"neutral"}:
        return {
            "status": "neutral_unmapped",
            "polarity": None,
            "topics": sorted(topics),
        }
    polarity = (
        "mixed"
        if "mixed" in polarities or {"positive", "negative"} <= polarities
        else "positive"
        if "positive" in polarities
        else "negative"
    )
    return {"status": "labeled", "polarity": polarity, "topics": sorted(topics)}


def f1(pairs, labels, multi=False):
    if not pairs:
        return None
    scores = []
    for label in labels:
        flags = [
            (label in a, label in b) if multi else (a == label, b == label)
            for a, b in pairs
        ]
        tp = sum(a and b for a, b in flags)
        fp = sum(not a and b for a, b in flags)
        fn = sum(a and not b for a, b in flags)
        scores.append(2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0)
    return sum(scores) / len(scores)


def metrics(items):
    usable = [i for i in items if i["gold"] and i["gold"]["status"] == "labeled"]
    compared = [i for i in usable if i["prediction"]["status"] == "labeled"]
    statuses = {
        s: sum(i["prediction"]["status"] == s for i in items)
        for s in ["labeled", "abstain", "uncertain", "neutral_unmapped", "failed"]
    }

    def calc(selected, penalize):
        polarity = [
            (i["gold"]["polarity"], i["prediction"]["polarity"]) for i in selected
        ]
        topics = [
            (
                i["gold"]["topics"],
                i["prediction"]["topics"]
                if i["prediction"]["status"] == "labeled"
                else [],
            )
            for i in selected
        ]
        return {
            "polarity_macro_f1": f1(polarity, ["positive", "negative", "mixed"]),
            "topic_macro_f1": f1(topics, TOPICS, True),
            "nonprediction_penalized": penalize,
        }

    return {
        "input_rows": len(items),
        "label_usable": len(usable),
        "label_unresolved": len(items) - len(usable),
        "label_coverage": len(usable) / len(items) if items else None,
        "prediction_statuses": statuses,
        "prediction_coverage_all_rows": statuses["labeled"] / len(items)
        if items
        else None,
        "compared_rows": len(compared),
        "comparison_coverage_usable_labels": len(compared) / len(usable)
        if usable
        else None,
        "conditional_agreement": calc(compared, False),
        "coverage_penalized_agreement": calc(usable, True),
    }


def score(rows, labels, predictions, issues):
    sources = {r["review_id"]: r for r in rows}
    gold = {r["review_id"]: r for r in labels}
    preds = {r["review_id"]: r for r in predictions}
    if (
        len(sources) != len(rows)
        or len(gold) != len(labels)
        or len(preds) != len(predictions)
        or set(sources) != set(gold)
        or set(sources) != set(preds)
    ):
        raise ValueError("identity/coverage mismatch")
    issue_ids = {i["review_id"] for i in issues}
    items = []
    for key, row in sources.items():
        if gold[key]["text"] != row["text"] or gold[key]["split"] != row["split"]:
            raise ValueError("label input binding mismatch")
        p = preds[key]
        items.append(
            {
                "review_id": key,
                "split": row["split"],
                "gold": gold[key]["label"],
                "prediction": project(p["guarded"])
                if p["status"] == "completed"
                else {"status": "failed", "polarity": None, "topics": []},
            }
        )
    return {
        "status": "AI_silver_agreement_only",
        "accuracy_claim": False,
        "new_blind_test": False,
        "splits": {
            split: metrics([i for i in items if i["split"] == split])
            for split in ["development", "validation", "holdout"]
        },
        "semantic_issue_subset": {
            split: metrics(
                [
                    i
                    for i in items
                    if i["split"] == split and i["review_id"] in issue_ids
                ]
            )
            for split in ["development", "validation", "holdout"]
        },
        "semantic_issue_ids": sorted(issue_ids),
        "all_rows": metrics(items),
        "projected_predictions": [
            {k: v for k, v in i.items() if k != "gold"} for i in items
        ],
        "unverified_gates": [
            "original retrieval recall",
            "tool differences",
            "evidence-driven plan changes",
            "independent adjudication",
            "full production acceptance",
        ],
        "unrepresented_product_topics": ["appearance", "service"],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ["run", "input", "silver", "issues", "output"]:
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    seal = json.loads((a.run / "prediction-seal.json").read_text())
    for name, field in [
        ("raw-predictions.json", "predictions_sha256"),
        ("execution-report.json", "execution_report_sha256"),
        ("plan.json", "plan_sha256"),
    ]:
        if sha(a.run / name) != seal[field]:
            raise ValueError("prediction seal mismatch")
    report = json.loads((a.run / "execution-report.json").read_text())
    plan = json.loads((a.run / "plan.json").read_text())
    if sha(a.input) != plan["input_sha256"]:
        raise ValueError("input binding mismatch")
    for ref in report["receipts"]:
        if sha(a.run / ref["path"]) != ref["sha256"]:
            raise ValueError("receipt mismatch")
    result = score(
        [json.loads(s) for s in a.input.read_text().splitlines()],
        json.loads(a.silver.read_text())["labels"],
        json.loads((a.run / "raw-predictions.json").read_text())["predictions"],
        json.loads(a.issues.read_text()),
    )
    result["bindings"] = {
        "prediction_seal_sha256": sha(a.run / "prediction-seal.json"),
        "silver_sha256": sha(a.silver),
        "issues_sha256": sha(a.issues),
    }
    with a.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
