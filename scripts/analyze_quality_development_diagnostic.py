"""Read sealed development receipts; never dispatch, score gold, or rewrite traces."""
import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path


def analyze(run, output):
    root = Path(__file__).resolve().parents[1]
    run, output = run.resolve(), output.resolve()
    if not run.is_relative_to(root / ".local/acceptance-artifacts") or output.exists():
        raise ValueError("owned run and unique analysis output required")
    paid = run / "paid"
    for name, digest in json.loads((paid / "seal.json").read_text()).items():
        if hashlib.sha256((paid / name).read_bytes()).hexdigest() != digest:
            raise ValueError("sealed receipt changed")
    sys.path.insert(0, str(root / "backend"))
    from app.agent.planner import PLANNER_SCHEMA
    from app.semantic.structured_response import validate_wire_value

    opening = json.loads((run / "budget-opening.json").read_text()) if (run / "budget-opening.json").exists() else []
    calls, peak_cost = [], Decimal(0)
    for reservation in sorted(paid.glob("reservation-*.json")):
        item = json.loads(reservation.read_text())
        index = reservation.stem.removeprefix("reservation-")
        response_path = paid / f"response-{index}.json"
        item["response_received"] = response_path.exists()
        if response_path.exists():
            response = json.loads(response_path.read_text())
            usage = response.get("usage", {})
            cost = (Decimal(usage["input_tokens"]) * Decimal(".30") + Decimal(usage["output_tokens"]) * Decimal("1.20")) / 1_000_000
            peak_cost += cost
            item.update(status=response["status"], usage=usage, peak_cache_miss_estimate_usd=str(cost))
            if item["kind"] == "planner":
                text = "".join(part["text"] for message in response.get("output", []) for part in message.get("content", []) if part.get("type") == "output_text")
                try:
                    body = json.loads(text)
                    validate_wire_value(body, PLANNER_SCHEMA)
                    item["current_wire_validation"] = "passed"
                    json.loads(body["arguments"])
                    item["inner_arguments_json"] = "passed"
                except (ValueError, TypeError, KeyError) as exc:
                    item["current_contract_check_error"] = str(exc)
        calls.append(item)
    retrievals = []
    inputs = json.loads((run / "inputs.json").read_text())["cases"]
    for path in sorted(paid.glob("retrieval-*.json")):
        index = int(path.stem.removeprefix("retrieval-"))
        actual, case = json.loads(path.read_text()), inputs[index]
        documents = {doc["record_id"]: doc for doc in case["documents"]}
        hits = actual.get("items", [])
        citations = all(hit.get("record_id") in documents and hit.get("text") == documents[hit["record_id"]]["text"] for hit in hits)
        scope = actual["scope"]
        retrievals.append({"case_id": case["case_id"], "returned_record_ids": [hit.get("record_id") for hit in hits],
            "source_texts_equal_inputs": citations, "scope_equal_inputs": scope["asin"] == case["asin"] and scope["start"] == case["start"] and scope["end"] == case["end"],
            "judgment_status": actual["relevance_judgment"]["status"], "retrieval_complete": actual["retrieval_complete"]})
    reserved = sum((Decimal(item["reserved_usd"]) for item in [*opening, *calls]), Decimal(0))
    result = {"scope": "post-seal development observations; no independent labels or production acceptance",
        "cumulative_call_units": len(opening) + len(calls), "current_calls": len(calls), "opening_calls": len(opening),
        "cumulative_reserved_usd": str(reserved), "uncommitted_budget_usd": str(Decimal(".11") - reserved),
        "received_peak_cache_miss_estimate_usd": str(peak_cost), "invoice_amount": "unknown",
        "calls": calls, "retrievals": retrievals, "semantic_accuracy_certified": False,
        "analysis_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    with output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.run, args.output)
    print(json.dumps({key: result[key] for key in ["cumulative_call_units", "cumulative_reserved_usd", "uncommitted_budget_usd"]}))
