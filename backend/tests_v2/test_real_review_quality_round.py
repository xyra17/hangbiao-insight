"""Frozen real-review model runtime; independent labels are never read here."""

import csv
import io
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pytest

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.skipif(
    not os.getenv("QUALITY_REAL_REVIEW_RUN"), reason="explicit frozen real-review run required")]


def test_real_review_model_runtime(setup_scope, settings, monkeypatch):
    import openai
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.utils import timezone
    from quality_round_guard import RoundGuard, save
    from real_review_runtime_contract import declared_date_window
    from rest_framework.test import APIClient

    from app.semantic import factory
    from app.semantic.config import PRIMARY_SEMANTIC_CONFIG
    from app.semantic.model_gateway import DeepSeekResponsesGateway
    from app.semantic.schemas import ReviewInput
    from insight.ingestion.service import accept_upload, create_source, parse_batch, publish_batch
    from insight.metrics.facts import latest_facts, source_manifest
    from insight.models import Product, ProductMapping
    from insight.semantic.vector_candidates import build_index, index_scope
    from tests_v2 import test_s4_s6_runtime_acceptance as helpers

    run = Path(os.environ["QUALITY_REAL_REVIEW_RUN"]).resolve()
    paid = os.getenv("QUALITY_REAL_REVIEW_PAID") == "1"
    plan = json.loads((run / "plan.json").read_text())
    guard = RoundGuard(run, paid=paid, constructor=openai.OpenAI)
    monkeypatch.setattr(openai, "OpenAI", guard.client)
    if not paid:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-nonfunctional-placeholder")
    monkeypatch.setenv("EVIDENCE_VECTOR_INDEX_DIR", str(guard.directory / "indexes"))
    monkeypatch.setattr(helpers, "EVAL_PLANNER_PROVIDER", "deepseek")
    monkeypatch.setattr(timezone, "now", lambda: datetime.fromisoformat(plan["as_of"]))
    for name in ("build_agent_planner", "build_relevance_gateway"):
        original = getattr(factory, name)

        def wrapped(original=original):
            value = original()
            gateway = getattr(value, "gateway", value)
            if not paid:
                gateway.execution_origin = "controlled_fixture"
                gateway.provenance = "offline-real-review-protocol-fixture"
            return value

        monkeypatch.setattr(factory, name, wrapped)

    def read_csv(name):
        with (run / "inputs" / name).open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))

    corpus = read_csv("reviews.csv")
    queries = read_csv("queries.csv")
    investigations = read_csv("investigations.csv")
    semantic_rows = [json.loads(line) for line in (run / "inputs/prediction-input.jsonl").read_text().splitlines()]
    assert len(corpus) == 120 and len(queries) == 30 and len(investigations) == 48 and len(semantic_rows) == 300
    assert all(set(row) == {"review_id", "text"} for row in semantic_rows)
    repository = Path(plan["repository"])

    def read_boundary(event, args):
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.is_relative_to(repository) and not (
                path.is_relative_to(run) or path.is_relative_to(repository / "backend/.venv")
            ):
                raise PermissionError("real-review runtime cannot read labels, prior runs or live source")

    sys.addaudithook(read_boundary)
    users, shops = setup_scope
    shop = shops[0]
    settings.STORAGE_ROOT = guard.directory / "storage"
    # The index covers the declared replay scope, not only sampled extrema.
    # Otherwise direct searches work while the wider explicit investigation fails.
    output = {"classification": [], "retrieval": {}, "trajectories": []}
    error = None
    start = end = None
    try:
        start, end = declared_date_window(plan, corpus, investigations)
        for asin in sorted({row["child_asin"] for row in corpus}):
            product = Product.objects.create(shop=shop, child_asin=asin, seller_sku=asin + "-FBA", fulfillment="FBA")
            ProductMapping.objects.create(product=product, version=1, name="公开真实评论演示商品",
                effective_from=date(2017, 1, 1), reason="public review runtime; demo fulfillment mapping")
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(corpus[0]))
        writer.writeheader()
        writer.writerows(corpus)
        source = create_source(users["admin"], shop, {"name": "Frozen real public review corpus", "kind": "reviews",
            "is_primary": True, "grain": "product_record", "coverage_basis": "business_date",
            "column_mapping": {key: key for key in corpus[0]}, "ignored_columns": []}, "real-review-round")
        batch = accept_upload(users["admin"], source, SimpleUploadedFile("reviews.csv", stream.getvalue().encode()),
            {"coverage_start": date.fromisoformat(start), "coverage_end": date.fromisoformat(end),
             "generated_at": datetime.fromisoformat(plan["as_of"]), "declared_complete": False,
             "declared_asins": sorted({row["child_asin"] for row in corpus})}, "real-review-round")
        parse_batch(batch.pk, users["admin"])
        batch.refresh_from_db()
        if batch.error_rows or batch.valid_rows != 120:
            raise RuntimeError("real_review_import_failed")
        publish_batch(users["admin"], batch.pk, allow_partial=False, confirm_contract=True, request_id="real-review-round")
        manifest = source_manifest(shop)
        builds = []
        for product in Product.objects.filter(shop=shop):
            for kind in ("reviews", "support"):
                rows = latest_facts(manifest, kind, product.child_asin, product.pk)
                builds.append(build_index(rows, index_scope(shop, product.child_asin, product.pk, kind, manifest),
                                          start=date.fromisoformat(start), end=date.fromisoformat(end)))
        save(guard.directory / "index-builds.json", builds)
        api = APIClient()
        api.force_authenticate(users["admin"])
        if not plan.get("execution_stages"):
            for query in queries:
                guard.group = "retrieval:" + query["query_id"]
                started = time.perf_counter()
                response = api.post("/api/v1/evidence-search/", {"shop": str(shop.pk), "child_asin": query["child_asin"],
                    "product": str(Product.objects.get(shop=shop, child_asin=query["child_asin"]).pk),
                    "query": query["query"], "start": start, "end": end, "kind": "reviews", "limit": 10}, format="json")
                if response.status_code != 200:
                    raise RuntimeError("real_retrieval_api_failed")
                data = dict(response.data)
                data.update(latency_ms=(time.perf_counter() - started) * 1000, query=query)
                output["retrieval"][query["query_id"]] = data
                save(guard.directory / f"retrieval-{len(output['retrieval']):02}.json", data)
                stage = data["candidate_stage"]
                if guard.stopped or data.get("relevance_judgment", {}).get("error") or (
                    stage["query_groups"] and stage["semantic_candidates"]["status"] != "ready"
                ):
                    raise RuntimeError("real_retrieval_adapter_or_index_failed")
        for index, row in enumerate(investigations):
            guard.group = "investigation:" + row["scenario_id"]
            _, trace = helpers.capture_pair_trajectory(api, users, shop, row, index,
                max_steps=plan["budget"]["investigation_call_units"])
            if plan.get("business_contract"):
                trace["business_contract_version"] = plan["business_contract"]["version"]
            output["trajectories"].append(trace)
            save(guard.directory / f"trajectory-{index + 1:02}.json", trace)
            if plan.get("execution_stages") and index == 0:
                guard.unlock_remaining(trace)
            if any(item.get("candidate_stage", {}).get("semantic_candidates", {}).get("status") == "unsupported"
                   for item in trace.get("state_evidence", [])):
                raise RuntimeError("real_investigation_semantic_index_unsupported")
            if guard.stopped or trace["status"] not in {"completed", "needs_clarification"} or trace["job"]["status"] != "completed":
                raise RuntimeError("real_investigation_runtime_failed")
        if plan.get("execution_stages"):
            for query in queries:
                guard.group = "retrieval:" + query["query_id"]
                started = time.perf_counter()
                response = api.post("/api/v1/evidence-search/", {"shop": str(shop.pk), "child_asin": query["child_asin"],
                    "product": str(Product.objects.get(shop=shop, child_asin=query["child_asin"]).pk),
                    "query": query["query"], "start": start, "end": end, "kind": "reviews", "limit": 10}, format="json")
                if response.status_code != 200:
                    raise RuntimeError("real_retrieval_api_failed")
                data = dict(response.data)
                data.update(latency_ms=(time.perf_counter() - started) * 1000, query=query)
                output["retrieval"][query["query_id"]] = data
                save(guard.directory / f"retrieval-{len(output['retrieval']):02}.json", data)
                stage = data["candidate_stage"]
                if guard.stopped or data.get("relevance_judgment", {}).get("error") or (
                    stage["query_groups"] and stage["semantic_candidates"]["status"] != "ready"
                ):
                    raise RuntimeError("real_retrieval_adapter_or_index_failed")
        config = PRIMARY_SEMANTIC_CONFIG.model_copy(update={"model": "deepseek-flash", "prompt_version": plan.get("semantic_prompt_version", "semantic-prompt-v1.2"),
            "reasoning_effort": "none", "max_output_tokens": 1200, "max_attempts": 1, "timeout_seconds": 30})
        service = factory.build_semantic_service(run / "taxonomy.json", gateway=DeepSeekResponsesGateway(config=config))
        for index, row in enumerate(semantic_rows):
            guard.group = "classification:" + row["review_id"]
            result = service.extract(ReviewInput(review_id="runtime-review", product_id="runtime-product", text=row["text"]))
            value = {"review_id": row["review_id"], "guarded": result.model_dump(mode="json")}
            output["classification"].append(value)
            save(guard.directory / f"classification-{index + 1:03}.json", value)
            if result.rejected_count:
                raise ValueError("semantic_output_contract_rejected")
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)[:300]
    finally:
        save(guard.directory / "runtime.json", {"trajectories": output["trajectories"], "real_provider_response": paid,
            "scope": "real public reviews; 120 document quality corpus, not full-corpus business recall"})
        save(guard.directory / "retrieval-runtime.json", {"queries": queries, "per_query": output["retrieval"],
             "real_provider_response": paid, "date_scope": {"start": start, "end": end}, "independent_qrels": "pending_user"})
        save(guard.directory / "raw-predictions.json", {"predictions": output["classification"], "real_provider_response": paid})
        summary = guard.finish({key: len(value) for key, value in output.items()}, error)
    assert not error and not guard.stopped, summary
    assert summary["coverage"] == {"classification": 300, "retrieval": 30, "trajectories": 48}
