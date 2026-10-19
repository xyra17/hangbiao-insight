from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from ..repositories.claim_store import BusinessClaim, ClaimStore
from .factory import build_semantic_service
from .model_gateway import ModelGatewayError
from .schemas import ReviewInput


def _gateway_failure(exc: Exception) -> bool:
    """Return True for provider/auth/quota failures that should trip the circuit breaker."""
    if isinstance(exc, ModelGatewayError):
        return True
    text = str(exc).casefold()
    return any(
        token in text
        for token in (
            "api key",
            "_api_key",
            "authentication",
            "unauthorized",
            "quota",
            "rate limit",
            "insufficient balance",
            "credit",
        )
    )


def _blocked_status(exc: Exception) -> str:
    if "key" in str(exc).casefold() or "credential" in str(exc).casefold():
        return "blocked_missing_deepseek_key"
    return "blocked_model_gateway_error"


def run(
    input_path: Path,
    output_path: Path,
    taxonomy_path: Path,
    limit: int = 1000,
    run_id: str = "business-pilot",
    products=None,
    min_per_product: int = 1,
    max_workers: int = 4,
    gateway_error_threshold: int = 3,
):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = output_path.with_suffix(".checkpoint.json")
    done = set(json.loads(checkpoint.read_text()) if checkpoint.exists() else [])
    try:
        service = build_semantic_service(taxonomy_path)
    except Exception as exc:
        manifest = {
            "run_id": run_id,
            "input_hash": hashlib.sha256(input_path.read_bytes()).hexdigest(),
            "sample_rule": {
                "products": sorted(set(products or ["B07P9S6R9Y", "B0BYJ8ZVWH", "B01G0X56YU"])),
                "limit": limit,
                "min_per_product": min_per_product,
            },
            "processed": 0,
            "record_count": len(done),
            "error_count": 0,
            "elapsed_seconds": 0.0,
            "status": _blocked_status(exc),
            "blocked_reason": type(exc).__name__,
            "blocked_message": str(exc)[:240],
        }
        (output_path.parent / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        return manifest
    store = ClaimStore()
    products = set(products or ["B07P9S6R9Y", "B0BYJ8ZVWH", "B01G0X56YU"])
    selected = 0
    errors = 0
    started_all = time.perf_counter()
    rows = []
    with input_path.open(encoding="utf-8-sig") as src:
        for r in csv.DictReader(src):
            if len(rows) >= limit:
                break
            if r.get("sku") in products and r.get("review_id") not in done:
                rows.append(r)

    def extract_row(r):
        review = ReviewInput(
            review_id=r["review_id"],
            product_id=r["sku"],
            title=r.get("title", ""),
            text=r.get("text", ""),
            rating=float(r.get("rating") or 0),
            created_at=r.get("created_at"),
        )
        return r, service.extract(review)

    with (
        output_path.open("a", encoding="utf-8") as out,
        ThreadPoolExecutor(max_workers=max(1, min(max_workers, 8))) as pool,
    ):
        iterator = iter(rows)
        inflight = {}
        gateway_errors = 0
        blocked_reason = None

        def submit_one() -> bool:
            try:
                row = next(iterator)
            except StopIteration:
                return False
            inflight[pool.submit(extract_row, row)] = row
            return True

        for _ in range(max(1, min(max_workers, 8))):
            if not submit_one():
                break
        while inflight:
            ready, _ = wait(set(inflight), timeout=180, return_when=FIRST_COMPLETED)
            if not ready:
                for fut, row in list(inflight.items()):
                    fut.cancel()
                    errors += 1
                    with (output_path.parent / "errors.jsonl").open("a") as ef:
                        ef.write(
                            json.dumps({"review_id": row.get("review_id"), "error_type": "TimeoutError"})
                            + "\n"
                        )
                    done.add(row.get("review_id"))
                    selected += 1
                inflight.clear()
                break
            for fut in ready:
                row = inflight.pop(fut)
                try:
                    row, result = fut.result()
                    for p in result.predictions:
                        c = BusinessClaim(
                            claim_id=p.prediction_id,
                            review_id=p.review_id,
                            product_id=p.product_id,
                            created_at=p.created_at,
                            primary_topic=p.primary_topic,
                            secondary_topic=p.secondary_topic,
                            polarity=p.polarity,
                            evidence_span=p.evidence_span,
                            confidence=0.8 if p.prediction_status == "accepted" else 0.2,
                            prediction_status=p.prediction_status,
                            semantic_version=p.semantic_version,
                            model_name=p.model_name,
                            prompt_version=p.prompt_version,
                            taxonomy_version=p.taxonomy_version,
                            run_id=run_id,
                            quality_flags=p.quality_flags,
                        )
                        store.upsert(c)
                        out.write(c.model_dump_json() + "\n")
                except Exception as exc:
                    errors += 1
                    gateway = _gateway_failure(exc)
                    if gateway:
                        gateway_errors += 1
                        blocked_reason = _blocked_status(exc)
                    with (output_path.parent / "errors.jsonl").open("a") as ef:
                        ef.write(
                            json.dumps(
                                {
                                    "review_id": row.get("review_id"),
                                    "error_type": type(exc).__name__,
                                    "gateway_failure": gateway,
                                    "message": str(exc)[:240],
                                }
                            )
                            + "\n"
                        )
                done.add(row.get("review_id"))
                tmp = checkpoint.with_suffix(".tmp")
                tmp.write_text(json.dumps(sorted(done)))
                os.replace(tmp, checkpoint)
                selected += 1
                if blocked_reason and gateway_errors >= max(1, gateway_error_threshold):
                    for remaining in inflight:
                        remaining.cancel()
                    inflight.clear()
                    break
                submit_one()
            if blocked_reason and gateway_errors >= max(1, gateway_error_threshold):
                break
    status = (
        blocked_reason
        if blocked_reason and gateway_errors >= max(1, gateway_error_threshold)
        else ("completed_with_errors" if errors else "completed")
    )
    manifest = {
        "run_id": run_id,
        "input_hash": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "sample_rule": {"products": sorted(products), "limit": limit, "min_per_product": min_per_product},
        "processed": selected,
        "record_count": len(done),
        "error_count": errors,
        "gateway_error_count": gateway_errors,
        "elapsed_seconds": round(time.perf_counter() - started_all, 3),
        "status": status,
    }
    if status.startswith("blocked_"):
        manifest["blocked_reason"] = blocked_reason
    (output_path.parent / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input", dest="input_path", type=Path, required=True)
    p.add_argument("--output", dest="output_path", type=Path, required=True)
    p.add_argument("--taxonomy", dest="taxonomy_path", type=Path, required=True)
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--run-id", default="business-pilot")
    p.add_argument("--products", nargs="+")
    p.add_argument("--max-workers", type=int, default=4)
    print(run(**vars(p.parse_args())))
