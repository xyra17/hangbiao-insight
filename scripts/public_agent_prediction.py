"""Label-isolated worker for the existing production semantic service.

No label, scorer, annotation receipt or source dataset imports. Run only from a
prepared snapshot. Paid dispatch requires a separately bound approval file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from time import perf_counter


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def install_read_boundary(repository, run):
    repository, run = Path(repository).resolve(), Path(run).resolve()
    venv = repository / "backend/.venv"

    def audit(event, args):
        if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(args[0])).resolve()
        if path.is_relative_to(repository) and not (
            path.is_relative_to(run) or path.is_relative_to(venv)
        ):
            raise PermissionError(
                "prediction worker cannot read repository data outside its safe snapshot"
            )

    sys.addaudithook(audit)


def review_input(row):
    from app.semantic.schemas import ReviewInput

    if set(row) != {"review_id", "text"}:
        raise ValueError("prediction input must contain only review_id/text")
    # Matches ProviderAdapter's runtime identity; no source ID leaves the process.
    return ReviewInput(
        review_id="runtime-review", product_id="runtime-product", text=row["text"]
    )


def extract_once(service, row):
    """Treat a rejected output contract as a failed attempt, never a success."""
    guarded = service.extract(review_input(row))
    value = guarded.model_dump(mode="json")
    if value.get("rejected_count", 0):
        raise ValueError("semantic_output_contract_rejected")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--paid", action="store_true")
    args = parser.parse_args()
    run = args.run.resolve()
    plan = json.loads((run / "plan.json").read_text())
    for ref in plan["runtime_references"]:
        if sha(run / ref["path"]) != ref["sha256"]:
            raise ValueError("runtime snapshot mismatch")
    install_read_boundary(plan["repository"], run)
    sys.path.insert(0, str(run / "snapshot/backend"))
    from app.semantic.config import PRIMARY_SEMANTIC_CONFIG
    from app.semantic.factory import build_semantic_service
    from app.semantic.model_gateway import DeepSeekResponsesGateway, ModelGatewayError
    from app.semantic.parser import SemanticParseError
    from app.semantic.prompt_builder import PromptBuilder
    from app.semantic.taxonomy import TaxonomyLoader

    config = PRIMARY_SEMANTIC_CONFIG.model_copy(
        update={
            "model": "deepseek-flash",
            "max_attempts": 1,
            "timeout_seconds": 30,
            "prompt_version": "semantic-prompt-v1.2",
        }
    )
    rows = [
        json.loads(line)
        for line in (run / "prediction-input.jsonl").read_text().splitlines()
    ]
    builder = PromptBuilder(
        TaxonomyLoader.load(run / "taxonomy.json"), prompt_version=config.prompt_version
    )
    requests = []
    for row in rows:
        prompt = builder.build(review_input(row))
        body = {
            "model": config.model,
            "input": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "reasoning": {"effort": config.reasoning_effort},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "review_semantic_extraction",
                    "schema": prompt.response_schema,
                    "strict": True,
                }
            },
            "store": False,
            "max_output_tokens": plan["max_output_tokens"],
        }
        envelope = len(json.dumps(body, ensure_ascii=False).encode()) + 2048
        if envelope > plan["max_request_bytes"]:
            raise ValueError("request envelope exceeds frozen bound")
        requests.append((row, body, envelope))
    maximum = max(r[2] for r in requests)
    if not args.paid:
        save(
            run / "dry-run.json",
            {
                "status": "dry_run_only",
                "model_calls": 0,
                "rows": len(rows),
                "planned_calls": len(rows),
                "max_observed_request_envelope": maximum,
                "model": config.model,
                "prompt_version": config.prompt_version,
                "no_labels_read": True,
            },
        )
        return
    approval = json.loads((run / "approval.json").read_text())
    if approval != {
        "approved": True,
        "plan_sha256": sha(run / "plan.json"),
        "max_total_usd": "1.20",
        "max_call_usd": "0.004",
        "transfer_confirmed": True,
    }:
        raise ValueError("new plan-specific budget/transfer approval required")
    if (run / "execution-report.json").exists() or (run / "receipts").exists():
        raise ValueError("no redispatch or automatic retry; unique execution required")
    import httpx
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=config.base_url,
        max_retries=0,
        timeout=30,
        http_client=httpx.Client(trust_env=False),
    )
    (run / "receipts").mkdir()
    reserved = Decimal(0)
    predictions = []

    class CappedClient:
        @property
        def responses(self):
            return self

        def create(self, **kwargs):
            nonlocal reserved
            row, expected, envelope = current
            kwargs["max_output_tokens"] = plan["max_output_tokens"]
            kwargs["text"]["format"]["strict"] = True
            if kwargs != expected:
                raise ValueError("production request differs from dry-run contract")
            bound = (
                Decimal(envelope) * Decimal("0.30")
                + Decimal(plan["max_output_tokens"]) * Decimal("1.20")
            ) / 1000000
            if bound > Decimal("0.004") or reserved + bound > Decimal("1.20"):
                raise ValueError("budget hard limit exceeded")
            reserved += bound
            path = run / "receipts" / f"{len(predictions) + 1:04}.json"
            receipt = {
                "review_id": row["review_id"],
                "request": kwargs,
                "reserved_upper_usd": str(bound),
                "status": "dispatched_unknown",
                "requested_model": config.model,
            }
            save(path, receipt)  # durable reservation before network dispatch
            start = perf_counter()
            try:
                response = client.responses.create(**kwargs)
                receipt.update(
                    status="response_received",
                    response=response.model_dump(mode="json"),
                    reported_model=getattr(response, "model", None),
                    response_id=getattr(response, "id", None),
                )
                return response
            except Exception as exc:
                receipt.update(
                    status="failed_unknown_charge", error_type=type(exc).__name__
                )
                raise
            finally:
                receipt["latency_ms"] = (perf_counter() - start) * 1000
                path.write_text(
                    json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
                )

    gateway = DeepSeekResponsesGateway(config=config, client=CappedClient())
    service = build_semantic_service(run / "taxonomy.json", gateway=gateway)
    stopped = None
    for current in requests:
        row, _, _ = current
        try:
            guarded = extract_once(service, row)
            result = {
                "review_id": row["review_id"],
                "status": "completed",
                "guarded": guarded,
            }
        except (ModelGatewayError, SemanticParseError, ValueError) as exc:
            result = {
                "review_id": row["review_id"],
                "status": "failed",
                "error_type": type(exc).__name__,
            }
            stopped = type(exc).__name__
        predictions.append(result)
        if stopped:
            break
    save(run / "raw-predictions.json", {"predictions": predictions})
    save(
        run / "execution-report.json",
        {
            "status": "stopped_on_first_failure" if stopped else "executed",
            "stop_reason": stopped,
            "model": config.model,
            "plan_sha256": sha(run / "plan.json"),
            "reserved_upper_usd": str(reserved),
            "predictions_sha256": sha(run / "raw-predictions.json"),
            "receipts": [
                {"path": str(p.relative_to(run)), "sha256": sha(p)}
                for p in sorted((run / "receipts").glob("*.json"))
            ],
            "rows": len(rows),
            "attempted_rows": len(predictions),
            "unattempted_rows": len(rows) - len(predictions),
            "max_attempts": 1,
            "supplier_invoice_verified": False,
        },
    )
    save(
        run / "prediction-seal.json",
        {
            "execution_report_sha256": sha(run / "execution-report.json"),
            "predictions_sha256": sha(run / "raw-predictions.json"),
            "plan_sha256": sha(run / "plan.json"),
        },
    )


if __name__ == "__main__":
    main()
