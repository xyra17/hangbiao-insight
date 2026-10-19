"""Post-seal structural trial report; no silver labels or semantic scores."""

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize(run):
    seal = json.loads((run / "prediction-seal.json").read_text())
    for name, key in [
        ("plan.json", "plan_sha256"),
        ("raw-results.json", "results_sha256"),
        ("execution-report.json", "execution_report_sha256"),
    ]:
        if sha(run / name) != seal[key]:
            raise ValueError("prediction seal mismatch")
    plan = json.loads((run / "plan.json").read_text())
    for ref in plan["runtime_references"]:
        if sha(run / ref["path"]) != ref["sha256"]:
            raise ValueError("runtime reference mismatch")
    baselines = json.loads((run / "sampling-baseline.json").read_text())["sample"]
    results = json.loads((run / "raw-results.json").read_text())["results"]
    execution = json.loads((run / "execution-report.json").read_text())
    if len(results) != 20 or [r["review_id"] for r in results] != [
        r["review_id"] for r in baselines
    ]:
        raise ValueError("sample binding mismatch")
    if len(execution["receipts"]) != 20:
        raise ValueError("exactly one dispatch per sample required")
    groups = {
        name: {
            "rows": 0,
            "provider_completed_nonempty": 0,
            "strict_parser_success": 0,
            "accepted_claims": 0,
            "rejected_claims": 0,
            "empty_claim_rows": 0,
            "failures": Counter(),
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "reasoning_usage_unknown_rows": 0,
            "latencies_ms": [],
        }
        for name in ["truncated", "completed"]
    }
    reserved = Decimal(0)
    known = Decimal(0)
    unknown = Decimal(0)
    models = Counter()
    remaining = []
    for baseline, result, ref in zip(
        baselines, results, execution["receipts"], strict=True
    ):
        path = run / ref["path"]
        if sha(path) != ref["sha256"]:
            raise ValueError("receipt mismatch")
        d = json.loads(path.read_text())
        request = d["request"]
        assert (
            baseline["split"] == "development"
            and result["review_id"] == d["review_id"] == baseline["review_id"]
        )
        assert (
            request["reasoning"] == {"effort": "none"}
            and request["model"] == "deepseek-flash"
            and request["max_output_tokens"] == 1200
        )
        old_request = {**request, "reasoning": {"effort": "low"}}
        assert (
            hashlib.sha256(
                json.dumps(old_request, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            == baseline["baseline_request_sha256"]
        )
        envelope = len(json.dumps(request, ensure_ascii=False).encode()) + 2048
        bound = (
            Decimal(envelope) * Decimal("0.30") + Decimal(1200) * Decimal("1.20")
        ) / 1000000
        assert (
            envelope <= 8000
            and bound == Decimal(d["reserved_upper_usd"])
            and bound <= Decimal("0.004")
        )
        reserved += bound
        g = groups[baseline["stratum"]]
        g["rows"] += 1
        g["latencies_ms"].append(d["latency_ms"])
        response = d.get("response", {})
        usage = response.get("usage")
        output_text = "".join(
            c.get("text", "")
            for item in response.get("output", [])
            for c in item.get("content", [])
            if c.get("type") == "output_text"
        )
        g["provider_completed_nonempty"] += response.get(
            "status"
        ) == "completed" and bool(output_text.strip())
        if usage:
            i, o = usage["input_tokens"], usage["output_tokens"]
            reported_reasoning = (usage.get("output_tokens_details") or {}).get(
                "reasoning_tokens"
            )
            g["reasoning_usage_unknown_rows"] += reported_reasoning is None
            reasoning = reported_reasoning if reported_reasoning is not None else 0
            cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
            assert (
                0 <= i <= envelope
                and 0 <= o <= 1200
                and 0 <= reasoning <= o
                and 0 <= cached <= i
            )
            for key, v in [
                ("input_tokens", i),
                ("output_tokens", o),
                ("reasoning_tokens", reasoning),
                ("cached_tokens", cached),
            ]:
                g[key] += v
            known += (
                Decimal(i) * Decimal("0.30") + Decimal(o) * Decimal("1.20")
            ) / 1000000
            models[response.get("model")] += 1
        else:
            unknown += bound
            g["reasoning_usage_unknown_rows"] += 1
        if result["status"] == "parsed":
            g["strict_parser_success"] += 1
            guarded = result["guarded"]
            g["accepted_claims"] += guarded["accepted_count"]
            g["rejected_claims"] += guarded["rejected_count"]
            g["empty_claim_rows"] += not guarded["predictions"]
            assert response.get("status") == "completed"
        else:
            g["failures"][result["error_code"]] += 1
            remaining.append(
                {
                    "review_id": result["review_id"],
                    "stratum": baseline["stratum"],
                    "error_code": result["error_code"],
                    "receipt_sha256": ref["sha256"],
                }
            )
    assert reserved == Decimal(execution["reserved_upper_usd"]) and reserved <= Decimal(
        "0.08"
    )
    for g in groups.values():
        g["failures"] = dict(g["failures"])
        lat = sorted(g.pop("latencies_ms"))
        g["latency_ms"] = {
            "p50": lat[(len(lat) - 1) // 2],
            "p95": lat[(95 * len(lat) + 99) // 100 - 1],
            "max": max(lat),
        }
        for key in ["provider_completed_nonempty", "strict_parser_success"]:
            g[key + "_rate"] = g[key] / g["rows"]
    completed = sum(g["provider_completed_nonempty"] for g in groups.values())
    parsed = sum(g["strict_parser_success"] for g in groups.values())
    rejected = sum(g["rejected_claims"] for g in groups.values())
    thinking = sum(g["reasoning_tokens"] for g in groups.values())
    missing_reasoning_usage = sum(
        g["reasoning_usage_unknown_rows"] for g in groups.values()
    )
    meets = (
        completed == parsed == 20
        and rejected == thinking == missing_reasoning_usage == 0
        and dict(models) == {"deepseek-flash": 20}
    )
    return {
        "status": "trial_criteria_met" if meets else "trial_criteria_not_met",
        "rows": 20,
        "model_calls": 20,
        "strata": groups,
        "remaining_failures": remaining,
        "provider_completed_nonempty": completed,
        "strict_parser_success": parsed,
        "rejected_claims": rejected,
        "reported_reasoning_tokens": thinking,
        "reasoning_usage_unknown_rows": missing_reasoning_usage,
        "reported_models": dict(models),
        "billing": {
            "reserved_upper_usd": str(reserved),
            "known_usage_peak_no_discount_estimate_usd": str(known),
            "unknown_charge_reserved_upper_usd": str(unknown),
            "supplier_invoice_verified": False,
        },
        "only_request_parameter_changed": "reasoning.effort low -> none",
        "accuracy_claim": False,
        "full_300_proof": False,
        "small_development_trial_only": True,
        "plan_sha256": sha(run / "plan.json"),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = summarize(a.run.resolve())
    with a.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
