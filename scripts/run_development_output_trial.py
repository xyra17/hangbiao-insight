"""One approved, label-isolated development trial of reasoning=none."""

import argparse
import hashlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from time import perf_counter


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, data):
    with path.open("x") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--paid", action="store_true")
    a = p.parse_args()
    run = a.run.resolve()
    plan = json.loads((run / "plan.json").read_text())
    for ref in plan["runtime_references"]:
        if sha(run / ref["path"]) != ref["sha256"]:
            raise ValueError("frozen snapshot mismatch")
    repository = Path(plan["repository"]).resolve()

    def boundary(event, args):
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.is_relative_to(repository) and not (
                path.is_relative_to(run)
                or path.is_relative_to(repository / "backend/.venv")
            ):
                raise PermissionError(
                    "trial worker cannot read source runs, labels or other repository data"
                )

    sys.addaudithook(boundary)
    sys.path.insert(0, str(run / "snapshot/backend"))
    from app.semantic.config import PRIMARY_SEMANTIC_CONFIG
    from app.semantic.factory import build_semantic_service
    from app.semantic.model_gateway import DeepSeekResponsesGateway, ModelGatewayError
    from app.semantic.parser import SemanticParseError
    from app.semantic.prompt_builder import PromptBuilder
    from app.semantic.schemas import ReviewInput
    from app.semantic.taxonomy import TaxonomyLoader

    config = PRIMARY_SEMANTIC_CONFIG.model_copy(
        update={
            "model": "deepseek-flash",
            "prompt_version": "semantic-prompt-v1.2",
            "reasoning_effort": "none",
            "max_output_tokens": 1200,
            "max_attempts": 1,
            "timeout_seconds": 30,
        }
    )
    builder = PromptBuilder(
        TaxonomyLoader.load(run / "taxonomy.json"), prompt_version=config.prompt_version
    )
    rows = [
        json.loads(s) for s in (run / "prediction-input.jsonl").read_text().splitlines()
    ]
    if len(rows) != 20 or len({r["review_id"] for r in rows}) != 20:
        raise ValueError("exact frozen 20-row development trial required")
    requests = []
    baselines = {
        row["review_id"]: row
        for row in json.loads((run / "sampling-baseline.json").read_text())["sample"]
    }
    for row in rows:
        if set(row) != {"review_id", "text"}:
            raise ValueError("only safe text/id input allowed")
        review = ReviewInput(
            review_id="runtime-review", product_id="runtime-product", text=row["text"]
        )
        prompt = builder.build(review)
        request = {
            "model": config.model,
            "input": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "reasoning": {"effort": "none"},
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
        envelope = len(json.dumps(request, ensure_ascii=False).encode()) + 2048
        baseline = baselines[row["review_id"]]
        old_request = {**request, "reasoning": {"effort": "low"}}
        if (
            baseline["split"] != "development"
            or hashlib.sha256(
                json.dumps(old_request, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            != baseline["baseline_request_sha256"]
        ):
            raise ValueError(
                "candidate must differ from its development baseline only in reasoning effort"
            )
        if envelope > 8000:
            raise ValueError("request exceeds input cap")
        requests.append((row, review, request, envelope))
    if not a.paid:
        save(
            run / "dry-run.json",
            {
                "status": "dry_run_only",
                "rows": 20,
                "model_calls": 0,
                "candidate_changes": {"reasoning_effort": ["low", "none"]},
                "max_request_envelope": max(r[3] for r in requests),
                "planned_calls": 20,
            },
        )
        return
    approval = json.loads((run / "approval.json").read_text())
    if approval != {
        "approved": True,
        "plan_sha256": sha(run / "plan.json"),
        "max_total_usd": "0.08",
        "max_call_usd": "0.004",
        "transfer_confirmed": True,
    }:
        raise ValueError("new trial-specific budget and transfer confirmation required")
    if (run / "receipts").exists():
        raise ValueError("no redispatch, retries, or automatic sample expansion")
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
    results = []

    class CappedClient:
        @property
        def responses(self):
            return self

        def create(self, **kwargs):
            nonlocal reserved
            row, _review, expected, envelope = current
            kwargs["text"]["format"]["strict"] = True
            if kwargs != expected:
                raise ValueError("product request differs from frozen trial")
            bound = (
                Decimal(envelope) * Decimal("0.30") + Decimal(1200) * Decimal("1.20")
            ) / 1000000
            if bound > Decimal("0.004") or reserved + bound > Decimal("0.08"):
                raise ValueError("hard cost cap exceeded")
            reserved += bound
            receipt = {
                "review_id": row["review_id"],
                "requested_model": config.model,
                "request": kwargs,
                "status": "dispatched_unknown",
                "reserved_upper_usd": str(bound),
            }
            path = run / "receipts" / f"{len(results) + 1:04}.json"
            save(path, receipt)
            start = perf_counter()
            try:
                response = client.responses.create(**kwargs)
                receipt.update(
                    status="response_received",
                    reported_model=getattr(response, "model", None),
                    response_id=getattr(response, "id", None),
                    response=response.model_dump(mode="json"),
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

    service = build_semantic_service(
        run / "taxonomy.json",
        gateway=DeepSeekResponsesGateway(config=config, client=CappedClient()),
    )
    for current in requests:
        row, review, _, _ = current
        try:
            guarded = service.extract(review)
            result = {
                "review_id": row["review_id"],
                "status": "parsed",
                "guarded": guarded.model_dump(mode="json"),
            }
        except (ModelGatewayError, SemanticParseError, ValueError) as exc:
            result = {
                "review_id": row["review_id"],
                "status": "failed",
                "error_type": type(exc).__name__,
                "error_code": getattr(
                    exc,
                    "code",
                    "format_error"
                    if isinstance(exc, SemanticParseError)
                    else "transport_or_contract_error",
                ),
            }
        results.append(result)
    save(run / "raw-results.json", {"results": results})
    save(
        run / "execution-report.json",
        {
            "status": "executed",
            "rows": 20,
            "max_attempts": 1,
            "reserved_upper_usd": str(reserved),
            "plan_sha256": sha(run / "plan.json"),
            "results_sha256": sha(run / "raw-results.json"),
            "receipts": [
                {"path": str(p.relative_to(run)), "sha256": sha(p)}
                for p in sorted((run / "receipts").glob("*.json"))
            ],
        },
    )
    save(
        run / "prediction-seal.json",
        {
            "plan_sha256": sha(run / "plan.json"),
            "results_sha256": sha(run / "raw-results.json"),
            "execution_report_sha256": sha(run / "execution-report.json"),
        },
    )


if __name__ == "__main__":
    main()
