"""Opt-in whole candidate runtime. Never opens scoring labels, qrels or prior runs."""

import csv
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pytest

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.skipif(
    not os.getenv("QUALITY_UNIFIED_RUN"), reason="explicit frozen whole-round preparation required")]


def test_frozen_unified_candidate_runtime(setup_scope, settings, monkeypatch):
    import openai
    from quality_round_guard import RoundGuard, save
    from rest_framework.test import APIClient

    from app.semantic import factory
    from app.semantic.config import PRIMARY_SEMANTIC_CONFIG
    from app.semantic.model_gateway import DeepSeekResponsesGateway
    from app.semantic.schemas import ReviewInput
    from insight.metrics.facts import latest_facts, source_manifest
    from insight.models import Product
    from insight.semantic.vector_candidates import build_index, index_scope
    from tests_v2 import test_s4_s6_runtime_acceptance as helpers

    run = Path(os.environ["QUALITY_UNIFIED_RUN"]).resolve()
    paid = os.getenv("QUALITY_UNIFIED_PAID") == "1"
    plan = json.loads((run / "plan.json").read_text())
    guard = RoundGuard(run, paid=paid, constructor=openai.OpenAI)
    if not paid:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-nonfunctional-placeholder")
    monkeypatch.setattr(openai, "OpenAI", guard.client)
    monkeypatch.setenv("EVIDENCE_VECTOR_INDEX_DIR", str(guard.directory / "indexes"))
    monkeypatch.setattr(helpers, "EVAL_PLANNER_PROVIDER", "deepseek")
    # Mark substitutes explicitly even though the production transport/parsers execute.
    for name in ("build_agent_planner", "build_relevance_gateway"):
        original = getattr(factory, name)
        def wrapped(original=original):
            value = original()
            gateway = getattr(value, "gateway", value)
            if not paid:
                gateway.execution_origin = "controlled_fixture"
                gateway.provenance = "offline-unified-protocol-fixture"
            return value
        monkeypatch.setattr(factory, name, wrapped)
    from django.utils import timezone
    monkeypatch.setattr(timezone, "now", lambda: datetime.fromisoformat(plan["as_of"]))

    def read_csv(path):
        with path.open(newline="") as stream:
            return list(csv.DictReader(stream))
    queries = read_csv(run / "inputs/retrieval_runtime_queries.csv")
    paired = read_csv(run / "inputs/paired_runtime_inputs.csv")
    corpus = read_csv(run / "inputs/support_cases.csv")
    semantic_rows = [json.loads(line) for line in (run / "inputs/prediction-input.jsonl").read_text().splitlines()]
    assert len(queries) == 30 and len(paired) == 48 and len(semantic_rows) == 300
    for row in semantic_rows:
        assert set(row) == {"review_id", "text"}
    repository = Path(plan["repository"])
    def read_boundary(event, args):
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.is_relative_to(repository) and not (path.is_relative_to(run) or path.is_relative_to(repository / "backend/.venv")):
                raise PermissionError("unified runtime cannot read labels, prior runs or live source")
    sys.addaudithook(read_boundary)
    users, shops = setup_scope
    shop = shops[0]
    outputs = {"classification": [], "retrieval": {}, "trajectories": []}
    error = None
    try:
        _, _, first_day, last_day = helpers.create_runtime_facts(users, shop, corpus, settings, guard.directory)
        manifest = source_manifest(shop)
        builds = []
        for product in Product.objects.filter(shop=shop):
            for kind in ("reviews", "support"):
                rows = latest_facts(manifest, kind, product.child_asin, product.pk)
                if kind == "reviews":
                    rows = rows.filter(is_deleted=False)
                builds.append(build_index(rows, index_scope(shop, product.child_asin, product.pk, kind, manifest),
                    start=date(2026, 1, 1), end=date(2026, 12, 31)))
        save(guard.directory / "index-builds.json", builds)
        api = APIClient()
        api.force_authenticate(users["admin"])
        for query in queries:
            guard.group = "retrieval:" + query["query_id"]
            started = time.perf_counter()
            response = api.post("/api/v1/evidence-search/", {"shop": str(shop.pk), "child_asin": "D5X2600001",
                "product": str(Product.objects.get(shop=shop, child_asin="D5X2600001").pk), "query": query["query"],
                "start": str(first_day), "end": str(last_day), "kind": "support", "limit": 10}, format="json")
            if response.status_code != 200:
                raise RuntimeError("retrieval_api_failed")
            data = dict(response.data)
            data.update(latency_ms=(time.perf_counter() - started) * 1000,
                        record_ids=[i["record_id"] for i in data["items"] if i.get("match_kind") == "relevant_hit"],
                        context_record_ids=[i["record_id"] for i in data["items"] if i.get("match_kind") == "scope_context"])
            outputs["retrieval"][query["query_id"]] = data
            save(guard.directory / f"retrieval-{len(outputs['retrieval']):02}.json", data)
            if data.get("relevance_judgment", {}).get("error") or data["candidate_stage"]["semantic_candidates"]["status"] != "ready":
                raise RuntimeError("retrieval_adapter_or_index_failed")
        for index, row in enumerate(paired):
            guard.group = "investigation:" + row["scenario_id"]
            _, trace = helpers.capture_pair_trajectory(api, users, shop, row, index, max_steps=plan["budget"]["investigation_call_units"])
            outputs["trajectories"].append(trace)
            save(guard.directory / f"trajectory-{index + 1:02}.json", trace)
            if trace["status"] not in {"completed", "needs_clarification"} or trace["job"]["status"] != "completed":
                raise RuntimeError("investigation_runtime_failed")
        config = PRIMARY_SEMANTIC_CONFIG.model_copy(update={"model": "deepseek-flash", "prompt_version": "semantic-prompt-v1.2",
            "reasoning_effort": "none", "max_output_tokens": 1200, "max_attempts": 1, "timeout_seconds": 30})
        service = factory.build_semantic_service(run / "taxonomy.json", gateway=DeepSeekResponsesGateway(config=config))
        for index, row in enumerate(semantic_rows):
            guard.group = "classification:" + row["review_id"]
            result = service.extract(ReviewInput(review_id="runtime-review", product_id="runtime-product", text=row["text"]))
            output = {"review_id": row["review_id"], "status": "completed", "guarded": result.model_dump(mode="json")}
            outputs["classification"].append(output)
            save(guard.directory / f"classification-{index + 1:03}.json", output)
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)[:300]
    finally:
        save(guard.directory / "runtime.json", {"trajectories": outputs["trajectories"], "request_count": len(outputs["trajectories"]),
            "pair_count": 24, "output_schema_version": "investigation-output-v1", "as_of": plan["as_of"],
            "real_provider_response": paid, "scope": "seen synthetic regression, not new blind test"})
        save(guard.directory / "retrieval-runtime.json", {"queries": queries, "per_query": outputs["retrieval"],
            "real_provider_response": paid, "date_scope": {"start": str(first_day), "end": str(last_day)},
            "input_manifest_sha256": hashlib.sha256((run / "inputs/corpus_manifest.json").read_bytes()).hexdigest()})
        save(guard.directory / "raw-predictions.json", {"predictions": outputs["classification"], "real_provider_response": paid})
        summary = guard.finish({"classification": len(outputs["classification"]), "retrieval": len(outputs["retrieval"]),
                                "investigation": len(outputs["trajectories"])}, error)
    assert not error and not guard.stopped, summary
    assert summary["coverage"] == {"classification": 300, "retrieval": 30, "investigation": 48}
