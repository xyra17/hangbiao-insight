"""Read-only post-prediction receipt and payload audit; no silver label reads."""

import argparse
import hashlib
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(run):
    plan = json.loads((run / "plan.json").read_text())
    seal = json.loads((run / "prediction-seal.json").read_text())
    for name, key in [
        ("plan.json", "plan_sha256"),
        ("raw-predictions.json", "predictions_sha256"),
        ("execution-report.json", "execution_report_sha256"),
    ]:
        assert sha(run / name) == seal[key], name
    for ref in plan["runtime_references"]:
        assert sha(run / ref["path"]) == ref["sha256"], ref["path"]
    sys.path.insert(0, str(run / "snapshot/backend"))
    from app.semantic.prompt_builder import PromptBuilder
    from app.semantic.schemas import ReviewInput
    from app.semantic.taxonomy import TaxonomyLoader

    builder = PromptBuilder(
        TaxonomyLoader.load(run / "taxonomy.json"),
        prompt_version=plan["prompt_version"],
    )
    inputs = [
        json.loads(line)
        for line in (run / "prediction-input.jsonl").read_text().splitlines()
    ]
    predictions = json.loads((run / "raw-predictions.json").read_text())["predictions"]
    execution = json.loads((run / "execution-report.json").read_text())
    assert len(inputs) == len(predictions) == execution["rows"] == 300
    assert [r["review_id"] for r in inputs] == [r["review_id"] for r in predictions]
    assert execution["plan_sha256"] == sha(run / "plan.json") and execution[
        "predictions_sha256"
    ] == sha(run / "raw-predictions.json")
    references = execution["receipts"]
    assert len(references) == 300 and len({r["path"] for r in references}) == 300
    assert {r["path"] for r in references} == {
        str(p.relative_to(run)) for p in (run / "receipts").glob("*.json")
    }
    reserved = Decimal(0)
    usage_peak = Decimal(0)
    unknown_reservation = Decimal(0)
    statuses, reported, prediction_statuses, errors, guard_statuses = (
        Counter(),
        Counter(),
        Counter(),
        Counter(),
        Counter(),
    )
    latencies = []
    total_input = total_output = total_cached = 0
    response_statuses = Counter()
    for row, pred, ref in zip(inputs, predictions, references, strict=True):
        path = run / ref["path"]
        assert sha(path) == ref["sha256"]
        receipt = json.loads(path.read_text())
        assert (
            receipt["review_id"] == row["review_id"]
            and receipt["requested_model"] == plan["model"]
        )
        assert set(row) == {"review_id", "text"}
        prompt = builder.build(
            ReviewInput(
                review_id="runtime-review",
                product_id="runtime-product",
                text=row["text"],
            )
        )
        expected = {
            "model": plan["model"],
            "input": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "reasoning": {"effort": "low"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "review_semantic_extraction",
                    "schema": prompt.response_schema,
                    "strict": True,
                }
            },
            "store": False,
            "max_output_tokens": 1200,
        }
        assert receipt["request"] == expected
        size = len(json.dumps(expected, ensure_ascii=False).encode()) + 2048
        assert size <= 8000
        bound = (
            Decimal(size) * Decimal("0.30") + Decimal(1200) * Decimal("1.20")
        ) / 1000000
        assert bound == Decimal(receipt["reserved_upper_usd"]) and bound <= Decimal(
            "0.004"
        )
        reserved += bound
        statuses[receipt["status"]] += 1
        prediction_statuses[pred["status"]] += 1
        if pred.get("error_type"):
            errors[pred["error_type"]] += 1
        latencies.append(receipt["latency_ms"])
        response = receipt.get("response", {})
        usage = response.get("usage")
        if usage:
            i, o = usage["input_tokens"], usage["output_tokens"]
            assert (
                type(i) is int and type(o) is int and 0 <= i <= size and 0 <= o <= 1200
            )
            cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
            assert 0 <= cached <= i
            total_input += i
            total_output += o
            total_cached += cached
            usage_peak += (
                Decimal(i) * Decimal("0.30") + Decimal(o) * Decimal("1.20")
            ) / 1000000
            reported[receipt.get("reported_model")] += 1
            response_statuses[response.get("status")] += 1
            assert (
                receipt.get("response_id") == response["id"]
                and receipt.get("reported_model") == response["model"]
            )
        else:
            unknown_reservation += bound
        if pred["status"] == "completed":
            assert receipt["status"] == "response_received"
            guarded = pred["guarded"]
            assert len(guarded["predictions"]) == sum(
                guarded[k]
                for k in ["accepted_count", "review_required_count", "rejected_count"]
            )
            for c in guarded["predictions"]:
                guard_statuses[c["prediction_status"]] += 1
                assert c["raw_response_ref"] == receipt["response_id"]
                assert (
                    c["review_id"] == "runtime-review"
                    and c["product_id"] == "runtime-product"
                )
                if c["prediction_status"] == "accepted":
                    assert c["evidence_span"] in row["text"]
        else:
            assert pred["status"] == "failed"
    assert reserved == Decimal(execution["reserved_upper_usd"]) and reserved <= Decimal(
        "1.20"
    )
    ordered = sorted(latencies)
    return {
        "status": "passed",
        "planned_rows": 300,
        "attempted_requests": len(references),
        "max_attempts": 1,
        "receipt_statuses": dict(statuses),
        "prediction_statuses": dict(prediction_statuses),
        "prediction_errors": dict(errors),
        "claim_guard_statuses": dict(guard_statuses),
        "requested_model": plan["model"],
        "reported_models": dict(reported),
        "provider_response_statuses": dict(response_statuses),
        "usage": {
            "input_tokens": total_input,
            "cached_input_tokens": total_cached,
            "output_tokens_including_reasoning": total_output,
        },
        "billing": {
            "reserved_upper_usd": str(reserved),
            "known_usage_peak_no_discount_estimate_usd": str(usage_peak),
            "unknown_charge_reserved_upper_usd": str(unknown_reservation),
            "supplier_invoice_verified": False,
            "estimate_scope": "configured peak input/output rates; cache/off-peak discounts not applied; not actual vendor bill",
        },
        "latency_ms": {
            "p50": ordered[(len(ordered) - 1) // 2],
            "p95": ordered[(95 * len(ordered) + 99) // 100 - 1],
            "max": max(ordered),
            "serial_sum": sum(ordered),
        },
        "all_requests_equal_frozen_product_prompt_schema": True,
        "no_source_ids_splits_or_labels_in_payload": True,
        "all_payload_and_usage_bounds_verified": True,
        "prediction_seal_sha256": sha(run / "prediction-seal.json"),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    result = audit(args.run.resolve())
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ["prediction_seal_sha256"]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
