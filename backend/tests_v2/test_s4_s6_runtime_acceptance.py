import csv
import hashlib
import importlib.util
import inspect
import io
import json
import math
import os
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from insight.ingestion.service import accept_upload, create_source, parse_batch, publish_batch
from insight.investigation.planner import PlannerDecision
from insight.models import (
    InvestigationBudgetLedger,
    InvestigationRun,
    InvestigationToolCall,
    Job,
    Product,
    ProductMapping,
    SupportCase,
    WorkspaceModelReservation,
)
from insight.tasks import run_job

REPO = Path(__file__).resolve().parents[2]
PACK = REPO / "dataset" / "data" / "07_S4_S6独立评测_v1"
RUNTIME = PACK / "runtime_corpus_v4"
FORBIDDEN_RUNTIME_COLUMNS = {"topic", "qrels", "answerable", "expected_route", "expected_behavior", "expected_next_step"}
EVAL_PLANNER_PROVIDER = os.getenv("INVESTIGATION_EVAL_PROVIDER", "deterministic").strip().lower()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def clean_runtime_inputs() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    query_path = RUNTIME / "retrieval_runtime_queries.csv"
    pair_path = RUNTIME / "paired_runtime_inputs.csv"
    corpus_path = RUNTIME / "support_cases.csv"
    for path in (query_path, pair_path, corpus_path):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            fields = set(csv.DictReader(handle).fieldnames or [])
        assert not fields.intersection(FORBIDDEN_RUNTIME_COLUMNS)
    queries = read_csv(query_path)
    paired = read_csv(pair_path)
    corpus = read_csv(corpus_path)
    assert {"query", "query_id", "split"} <= set(queries[0])
    assert {
        "scenario_id", "query", "selected_child_asin", "evidence_child_asin", "split"
    } == set(paired[0])
    assert {
        "child_asin", "seller_sku", "fulfillment", "business_date", "record_id", "text", "status"
    } == set(corpus[0])
    manifest = json.loads((RUNTIME / "corpus_manifest.json").read_text(encoding="utf-8"))
    retrieval_rows = [row for row in corpus if row["child_asin"] == manifest["retrieval_scope_asin"]]
    assert len(queries) == 30
    assert len(retrieval_rows) == manifest["retrieval_document_count"] == 120
    assert len(paired) == manifest["paired_scenario_count"] == 48
    assert all(
        not row["selected_child_asin"]
        or len(row["selected_child_asin"]) == 10
        for row in paired
    )
    assert all(len(row["evidence_child_asin"]) == 10 for row in paired)
    return queries, paired, corpus


def create_runtime_facts(users, shop, corpus_rows, settings, tmp_path):
    settings.STORAGE_ROOT = tmp_path / "storage"
    asins = sorted({row["child_asin"] for row in corpus_rows})
    first_day = min(date.fromisoformat(row["business_date"]) for row in corpus_rows)
    last_day = max(date.fromisoformat(row["business_date"]) for row in corpus_rows)
    for asin in asins:
        product, _ = Product.objects.get_or_create(
            shop=shop,
            child_asin=asin,
            seller_sku=f"EVAL-{asin}",
            fulfillment="FBA",
        )
        ProductMapping.objects.get_or_create(
            product=product,
            version=1,
            defaults={
                "name": "合成程序评测商品",
                "effective_from": date(2026, 1, 1),
                "reason": "隔离的 S4/S6 合成程序评测",
            },
        )

    fields = ["child_asin", "seller_sku", "fulfillment", "business_date", "record_id", "text", "status"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(corpus_rows)
    source = create_source(
        users["admin"],
        shop,
        {
            "name": "S4/S6 Runtime Synthetic Support Corpus",
            "kind": "support",
            "is_primary": True,
            "grain": "product_record",
            "column_mapping": {field: field for field in fields},
            "ignored_columns": [],
            "coverage_basis": "business_date",
        },
        "s4-s6-runtime-evaluation",
    )
    upload = SimpleUploadedFile("runtime_support_cases.csv", stream.getvalue().encode(), content_type="text/csv")
    batch = accept_upload(
        users["admin"],
        source,
        upload,
        {
            "coverage_start": first_day,
            "coverage_end": last_day,
            "generated_at": datetime.combine(last_day, datetime.min.time(), UTC),
            "declared_complete": False,
            "declared_asins": asins,
        },
        "s4-s6-runtime-evaluation",
    )
    parse_batch(batch.pk, users["admin"])
    batch.refresh_from_db()
    assert batch.status == "preview" and batch.error_rows == 0, list(batch.rows.values_list("error", flat=True))
    publish_batch(
        users["admin"], batch.pk, allow_partial=False, confirm_contract=True, request_id="s4-s6-runtime-evaluation"
    )
    return source, batch, first_day, last_day


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    index = max(0, math.ceil(fraction * len(sorted_values)) - 1)
    return round(sorted_values[index], 3)


def summarize_retrieval(queries, outputs, qrels, first_day, last_day):
    positives: dict[str, set[str]] = {}
    for row in qrels:
        positives.setdefault(row["query_id"], set()).add(row["doc_id"])
    report = {"overall": {}, "splits": {}}
    for split in ("development", "validation", "test"):
        ids = [row["query_id"] for row in queries if row["split"] == split]
        relevant_total = sum(len(positives.get(query_id, set())) for query_id in ids)
        relevant_hits = sum(
            len(positives.get(query_id, set()).intersection(outputs[query_id]["record_ids"])) for query_id in ids
        )
        answerable_ids = [query_id for query_id in ids if positives.get(query_id)]
        no_answer_ids = [query_id for query_id in ids if not positives.get(query_id)]
        false_positive_counts = [
            sum(record_id not in positives[query_id] for record_id in outputs[query_id]["record_ids"])
            for query_id in answerable_ids
        ]
        no_answer_retrievals = sum(bool(outputs[query_id]["record_ids"]) for query_id in no_answer_ids)
        items = [item for query_id in ids for item in outputs[query_id]["items"] if item.get("match_kind", "relevant_hit") == "relevant_hit"]
        citation_valid = sum(bool(item.get("fact_id") and item.get("record_id") and item.get("raw_row_id")) for item in items)
        latencies = [outputs[query_id]["latency_ms"] for query_id in ids]
        report["splits"][split] = {
            "query_count": len(ids),
            "answerable_query_count": len(answerable_ids),
            "no_answer_query_count": len(no_answer_ids),
            "relevant_documents": relevant_total,
            "relevant_documents_retrieved_at_10": relevant_hits,
            "recall_at_10": round(relevant_hits / relevant_total, 4) if relevant_total else None,
            "false_positive_documents": sum(false_positive_counts),
            "answerable_queries_with_false_retrieval": sum(value > 0 for value in false_positive_counts),
            "no_answer_false_retrievals": no_answer_retrievals,
            "scope_context_documents": sum(len(outputs[query_id].get("context_record_ids", [])) for query_id in ids),
            "no_answer_false_retrieval_rate": round(no_answer_retrievals / len(no_answer_ids), 4) if no_answer_ids else None,
            "retrieved_documents": len(items),
            "citation_validity": round(citation_valid / len(items), 4) if items else None,
            "latency_ms": {
                "p50": percentile(latencies, 0.50),
                "p95": percentile(latencies, 0.95),
                "max": round(max(latencies), 3) if latencies else None,
            },
        }
    all_ids = [row["query_id"] for row in queries]
    all_relevant = sum(len(positives.get(query_id, set())) for query_id in all_ids)
    all_hits = sum(
        len(positives.get(query_id, set()).intersection(outputs[query_id]["record_ids"])) for query_id in all_ids
    )
    all_latencies = [outputs[query_id]["latency_ms"] for query_id in all_ids]
    report["overall"] = {
        "query_count": len(all_ids),
        "corpus_retrieval_documents": sum(
            row["child_asin"]
            == json.loads((RUNTIME / "corpus_manifest.json").read_text(encoding="utf-8"))["retrieval_scope_asin"]
            for row in read_csv(RUNTIME / "support_cases.csv")
        ),
        "recall_at_10": round(all_hits / all_relevant, 4) if all_relevant else None,
        "citation_validity": round(
            sum(bool(item.get("fact_id") and item.get("record_id") and item.get("raw_row_id")) for out in outputs.values() for item in out["items"])
            / max(1, sum(len(out["items"]) for out in outputs.values())),
            4,
        ),
        "latency_ms": {
            "p50": percentile(all_latencies, 0.50),
            "p95": percentile(all_latencies, 0.95),
            "max": round(max(all_latencies), 3) if all_latencies else None,
        },
        "date_scope": {"start": str(first_day), "end": str(last_day)},
    }
    return report


class FixedWorkflowPlanner:
    """A deliberately non-adaptive fixed baseline: snapshot, one text read, stop."""

    name = "fixed-workflow-snapshot-text-v1"

    def decide(self, *, query, evidence, history, available_tools):
        seen = {item.get("tool") or item.get("tool_name") for item in history}
        for tool in ("metric_snapshot", "text_evidence"):
            if tool not in seen and tool in available_tools:
                return PlannerDecision(
                    next_tool=tool,
                    hypothesis="固定基线按预设顺序读取快照和一次文本证据，不根据证据改路由。",
                    planner=self.name,
                    execution_origin="fixed_workflow",
                )
        return PlannerDecision(
            stop_reason="固定基线预设步骤已执行完毕。",
            hypothesis="固定流程完成，不追加自适应查询。",
            planner=self.name,
            execution_origin="fixed_workflow",
        )


def capture_pair_trajectory(client, users, shop, row, index, *, arm="deterministic_planner", max_steps=8):
    request_data = {
        "shop": str(shop.pk),
        "query": row["query"],
        "max_steps": max_steps,
        "planner_provider": EVAL_PLANNER_PROVIDER,
        "allow_internal_task": False,
        "idempotency_key": f"s4-s6-runtime-{arm}-{hashlib.sha256(row['scenario_id'].encode()).hexdigest()[:32]}",
    }
    selected_asin = row["selected_child_asin"].strip()
    if selected_asin:
        request_data["child_asin"] = selected_asin
        request_data["product"] = str(Product.objects.get(shop=shop, child_asin=selected_asin).pk)
    response = client.post("/api/v1/investigations/", request_data, format="json")
    assert response.status_code == 202, response.data
    job_id = response.data["id"]
    job_started = time.perf_counter()
    run_job(job_id)
    wall_duration_ms = (time.perf_counter() - job_started) * 1000
    job = Job.objects.get(pk=job_id)
    investigation_id = response.data["result"]["investigation_id"]
    run = InvestigationRun.objects.prefetch_related("steps", "tool_calls").get(pk=investigation_id)
    decisions = run.decisions if isinstance(run.decisions, list) else []
    actual_tools = list(run.tool_calls.order_by("sequence"))
    retrievals = [
        {
            "phase": item.get("phase", ""),
            "retrieval_id": item.get("retrieval_id", item.get("id")),
            "origin_decision_hash": item.get("origin_decision_hash"),
            "derived_from_retrieval_ids": item.get("derived_from_retrieval_ids", []),
            "query": item.get("query", ""),
            "evidence_context": item.get("evidence_context", "any"),
            "search_intent": item.get("search_intent"),
            "retrieval_complete": item.get("retrieval_complete", True),
            "scope": item.get("scope", {}),
            "record_ids": [hit.get("record_id") for hit in item.get("items", [])],
        }
        for item in (run.result or {}).get("evidence", (run.state or {}).get("evidence", []))
        if item.get("type") == "text_retrieval"
    ]
    trace = {
        "arm": arm,
        "scenario_id": row["scenario_id"],
        "investigation_id": str(run.pk),
        "wall_duration_ms": round(wall_duration_ms, 3),
        "status": run.status,
        "stop_reason": run.stop_reason,
        "query": row["query"],
        "content_query": run.state.get("content_query"),
        "requested_child_asin_scope": selected_asin,
        "resolved_child_asin_scope": run.child_asin,
        "decisions": decisions,
        "tools": [
            {
                "name": item.tool_name,
                "scope": item.scope,
                "authorization": item.authorization,
                "status": item.status,
                "output_refs": item.output_refs,
            }
            for item in actual_tools
        ],
        "retrievals": retrievals,
        "steps": list(run.steps.order_by("sequence").values("sequence", "node", "tool_name", "status", "evidence_refs")),
        "budget": {"max_steps": run.max_steps, "steps_used": run.steps_used, "remaining_units": run.budget_units},
        "model_usage": {
            "attempts": run.model_attempts,
            "tokens": run.model_tokens,
            "cost": str(run.model_cost),
        },
        "job": {"status": job.status, "error": job.error, "attempts": job.attempts},
        "result": run.result,
        "state_evidence": (run.state or {}).get("evidence", []),
    }
    from insight.investigation.evaluation import adapt_prediction

    predicted, metadata = adapt_prediction(trace)
    trace["evaluation_metadata"] = metadata
    return predicted, trace


def persisted_model_accounting(traces):
    """Summarize planner attempts and provider spend from durable receipts."""
    run_ids = [trace["investigation_id"] for trace in traces]
    entries = InvestigationBudgetLedger.objects.filter(run_id__in=run_ids, kind="model")
    reservations = list(WorkspaceModelReservation.objects.filter(investigation_entry__run_id__in=run_ids))
    dispatched_cost = sum(
        (row.actual_cost if row.actual_cost is not None else row.reserved_cost)
        for row in reservations
        if row.dispatch_count > 0
    )
    return {
        "planner_decision_attempts": entries.count(),
        "model_tokens": sum(entries.values_list("tokens", flat=True)),
        "model_cost": str(sum(entries.values_list("cost", flat=True), Decimal("0"))),
        "external_model_dispatches": sum(row.dispatch_count for row in reservations),
        "external_model_cost_actual_or_reserved": str(dispatched_cost),
        "external_model_unknown_receipts": sum(
            row.dispatch_count > 0 and row.actual_cost is None for row in reservations
        ),
    }


def summarize_trajectory_runtime(traces):
    durations = [trace["wall_duration_ms"] for trace in traces]
    retrievals = [item for trace in traces for item in trace["retrievals"]]
    adaptive = [item for item in retrievals if item["phase"] in {"counter_evidence", "model_follow_up"}]
    initial = [item for item in retrievals if item["phase"] == "initial"]
    return {
        "wall_duration_ms": {
            "total": round(sum(durations), 3),
            "p50_per_investigation": percentile(durations, 0.50),
            "p95_per_investigation": percentile(durations, 0.95),
            "max_per_investigation": round(max(durations), 3) if durations else None,
        },
        "retrieval_count": len(retrievals),
        "initial_retrieval_count": len(initial),
        "adaptive_retrieval_count": len(adaptive),
        "adaptive_retrieval_phases": {
            "counter_evidence": sum(item["phase"] == "counter_evidence" for item in adaptive),
            "model_follow_up": sum(item["phase"] == "model_follow_up" for item in adaptive),
        },
        "adaptive_count_excludes_initial_source_retrievals": True,
    }


def test_planner_comparison_blocks_unapproved_provider_before_factory(monkeypatch):
    from insight.investigation import planner

    monkeypatch.setenv("INSIGHT_ENABLED_MODEL_PROVIDERS", "deepseek")
    monkeypatch.delenv("INVESTIGATION_APPROVAL_REFERENCE", raising=False)
    monkeypatch.delenv("INVESTIGATION_REGION", raising=False)
    factory_calls = []
    monkeypatch.setattr(planner, "_external_planner", lambda *args, **kwargs: factory_calls.append(True))

    with pytest.raises(planner.PlannerUnavailable):
        planner.planner_for("deepseek")
    assert factory_calls == []


@pytest.mark.django_db(transaction=True)
def test_real_investigation_and_retrieval_acceptance_uses_project_apis(setup_scope, settings, tmp_path, monkeypatch):
    from insight.investigation.execution_config import (
        bind_observed_execution,
        configuration_snapshot,
        execution_identity,
        observed_text_parameters,
    )
    assert EVAL_PLANNER_PROVIDER == "deterministic", (
        "S4/S6 comparison is a zero-provider acceptance; real provider runs require a separate authorized task."
    )
    monkeypatch.setenv("INVESTIGATION_PLANNER_PROVIDER", "deterministic")
    monkeypatch.setenv("EVIDENCE_RELEVANCE_ENABLED", "0")
    monkeypatch.setenv("EVIDENCE_RELEVANCE_PROVIDER", "")
    monkeypatch.setenv("INSIGHT_ENABLED_MODEL_PROVIDERS", "")
    import httpx

    def reject_network(*args, **kwargs):
        raise AssertionError("S4/S6 free comparison must not make network requests")

    monkeypatch.setattr(httpx.Client, "send", reject_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", reject_network)
    execution_config = configuration_snapshot("deterministic")
    assert execution_config["relevance_enabled"] is False
    queries, paired_inputs, corpus = clean_runtime_inputs()
    manifest_path = RUNTIME / "corpus_manifest.json"
    runtime_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    users, shops = setup_scope
    shop = shops[0]
    source, batch, first_day, last_day = create_runtime_facts(users, shop, corpus, settings, tmp_path)

    api = APIClient()
    api.force_authenticate(users["admin"])
    retrieval_outputs = {}
    for query in queries:
        started = time.perf_counter()
        response = api.post(
            "/api/v1/evidence-search/",
            {
                "shop": str(shop.pk),
                "child_asin": "D5X2600001",
                "product": str(Product.objects.get(shop=shop, child_asin="D5X2600001").pk),
                "query": query["query"],
                "start": str(first_day),
                "end": str(last_day),
                "kind": "support",
                "limit": 10,
            },
            format="json",
        )
        latency = (time.perf_counter() - started) * 1000
        assert response.status_code == 200, response.data
        retrieval_outputs[query["query_id"]] = {
            "record_ids": [item["record_id"] for item in response.data["items"] if item.get("match_kind") == "relevant_hit"],
            "context_record_ids": [item["record_id"] for item in response.data["items"] if item.get("match_kind") == "scope_context"],
            "match_status": response.data["match_status"],
            "relevance_judgment": response.data.get("relevance_judgment", {"status": "legacy_unknown"}),
            "algorithm_manifest": response.data["algorithm_manifest"],
            "candidate_stage": response.data["candidate_stage"],
            "items": response.data["items"],
            "latency_ms": latency,
            "retrieval_version": response.data["retrieval_version"],
            "index_version": response.data["index_version"],
        }

    # qrels are intentionally opened only after all real retrieval API calls have completed.
    qrels = read_csv(PACK / "retrieval_qrels.csv")
    retrieval_summary = summarize_retrieval(queries, retrieval_outputs, qrels, first_day, last_day)
    retrieval_summary.update(
        {
            "status": "executed",
            "execution_path": "Django REST /api/v1/evidence-search/ with the published, metadata-cleaned runtime corpus",
            "runtime_corpus_version": runtime_manifest["version"],
            "runtime_corpus_manifest_sha256": runtime_manifest_sha256,
            "runtime_queries_sha256": runtime_manifest["files"]["retrieval_runtime_queries.csv"]["sha256"],
            "runtime_documents_sha256": runtime_manifest["files"]["support_cases.csv"]["sha256"],
            "requested_count": len(queries),
            "request_success_count": len(retrieval_outputs),
            "source_id": str(source.pk),
            "published_revision": source.revision,
            "batch_id": str(batch.pk),
            "batch_rows": batch.valid_rows,
            "corpus_manifest_sha256": runtime_manifest_sha256,
            "per_query": retrieval_outputs,
        }
    )
    from insight.metrics.facts import latest_facts, source_manifest
    from insight.semantic.indexing import INDEX_VERSION

    scope_product = Product.objects.get(shop=shop, child_asin="D5X2600001")
    scoped_facts = latest_facts(source_manifest(shop), "support", scope_product.child_asin, scope_product.pk).filter(
        business_date__range=(first_day, last_day)
    )
    retrieval_summary["fixture_integrity"] = {
        "published_support_fact_count": SupportCase.objects.filter(source=source).count(),
        "retrieval_scope_fact_count": scoped_facts.count(),
        "retrieval_scope_indexed_count": scoped_facts.filter(search_index_version=INDEX_VERSION, search_vector__isnull=False).count(),
        "source_manifest": source_manifest(shop),
    }
    assert len(retrieval_outputs) == 30 and batch.valid_rows == 168

    paired_predictions = []
    paired_traces = []
    for index, row in enumerate(paired_inputs):
        prediction, trace = capture_pair_trajectory(api, users, shop, row, index, arm="deterministic_planner")
        paired_predictions.append(prediction)
        paired_traces.append(trace)

    # Reuse the same project API, facts, runtime rows and step allowance for a
    # fixed two-read sequence. This is a test-only controller, not a product
    # planner choice or a claim that fixed reads have independent semantic truth.
    from insight.investigation import graph as graph_module
    from insight.investigation import planner as planner_module

    def no_counter_evidence_search(query, _results):
        return {
            "schema_version": "planner-comparison-counter-search-disabled-v1",
            "query": query,
            "evidence_context": "any",
            "optional_terms": [],
            "instruction": "Adaptive counter-evidence retrieval disabled for this controlled planner comparison.",
            "observed_contexts": [],
            "scope_policy": "inherit_authorized_frozen_scope",
        }

    monkeypatch.setattr(graph_module, "evidence_driven_counter_search", no_counter_evidence_search)

    production_planner_for = planner_module.planner_for

    def fixed_planner_for(provider=None):
        if (provider or "deterministic").strip().lower() in {"", "deterministic", "test"}:
            return FixedWorkflowPlanner()
        return production_planner_for(provider)

    monkeypatch.setattr(planner_module, "planner_for", fixed_planner_for)
    fixed_predictions = []
    fixed_traces = []
    for index, row in enumerate(paired_inputs):
        prediction, trace = capture_pair_trajectory(api, users, shop, row, index, arm="fixed_workflow")
        fixed_predictions.append(prediction)
        fixed_traces.append(trace)
    monkeypatch.setattr(planner_module, "planner_for", production_planner_for)

    # This is the scoring boundary: gold decisions become visible only after the actual graph completed.
    gold_path = PACK / "paired_investigation_scenarios.csv"
    gold_rows = read_csv(gold_path)
    spec = importlib.util.spec_from_file_location("paired_scorer", REPO / "dataset" / "score_paired_investigations.py")
    assert spec and spec.loader
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    score = scorer.score_rows(gold_rows, paired_predictions, mode="real_django_rest_api_investigation_graph")
    fixed_score = scorer.score_rows(gold_rows, fixed_predictions, mode="fixed_flow_django_rest_api_investigation_graph")
    expected_pair_labels = {}
    for row in gold_rows:
        expected_pair_labels[row["scenario_id"]] = row["pair_id"]
    trace_by_id = {row["scenario_id"]: row for row in paired_traces}
    pairs = {}
    for scenario_id, pair_id in expected_pair_labels.items():
        pairs.setdefault(pair_id, []).append(trace_by_id[scenario_id])
    pair_divergence = []
    for pair_id, pair_rows in sorted(pairs.items()):
        follow_up = [
            [json.dumps({"query": item["query"], "evidence_context": item["evidence_context"]}, sort_keys=True) for item in row["retrievals"] if item["phase"] == "counter_evidence"]
            for row in pair_rows
        ]
        executed_parameters = [observed_text_parameters(row) for row in pair_rows]
        post_evidence_parameters = [observed_text_parameters(row, after_evidence=True) for row in pair_rows]
        parameter_changed = len({tuple(value) for value in executed_parameters}) > 1
        post_evidence_changed = all(post_evidence_parameters) and len({tuple(value) for value in post_evidence_parameters}) > 1
        evidence = [
            {record_id for item in row["retrievals"] for record_id in item["record_ids"] if record_id}
            for row in pair_rows
        ]
        tool_traces = [[item["name"] for item in row["tools"]] for row in pair_rows]
        tool_names_changed = len({tuple(value) for value in tool_traces}) > 1
        follow_up_changed = all(follow_up) and len({tuple(value) for value in follow_up}) > 1
        retrieved_evidence_changed = len({frozenset(value) for value in evidence}) > 1
        pair_divergence.append(
            {
                "pair_id": pair_id,
                "status": "evidence_or_tool_parameter_diverged" if follow_up_changed or retrieved_evidence_changed or tool_names_changed else "no_observed_divergence",
                "tool_names_changed": tool_names_changed,
                "executed_search_parameter_changed": parameter_changed,
                "post_evidence_executed_parameter_changed": post_evidence_changed,
                "follow_up_query_changed_after_evidence": follow_up_changed,
                "follow_up_parameter_changed_with_evidence": (follow_up_changed or post_evidence_changed) and retrieved_evidence_changed,
                "retrieved_evidence_changed": retrieved_evidence_changed,
                "tool_names": tool_traces,
            }
        )
    planner_accounting = persisted_model_accounting(paired_traces)
    fixed_accounting = persisted_model_accounting(fixed_traces)
    planner_runtime = summarize_trajectory_runtime(paired_traces)
    fixed_runtime = summarize_trajectory_runtime(fixed_traces)
    planner_call_count = InvestigationToolCall.objects.filter(
        run_id__in=[row["investigation_id"] for row in paired_traces]
    ).count()
    fixed_call_count = InvestigationToolCall.objects.filter(
        run_id__in=[row["investigation_id"] for row in fixed_traces]
    ).count()
    paired_summary = {
        "status": "executed",
        "execution_path": f"POST /api/v1/investigations/, {EVAL_PLANNER_PROVIDER} project planner, durable job runner and real text_evidence tool",
        "runtime_corpus_version": runtime_manifest["version"],
        "runtime_corpus_manifest_sha256": runtime_manifest_sha256,
        "runtime_pair_inputs_sha256": runtime_manifest["files"]["paired_runtime_inputs.csv"]["sha256"],
        "runtime_documents_sha256": runtime_manifest["files"]["support_cases.csv"]["sha256"],
        "runtime_fixture_context_sha256": runtime_manifest["runtime_context_sha256"],
        "executed_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "planner_provider": EVAL_PLANNER_PROVIDER,
        "adaptive_counter_evidence_search": "enabled_for_production_deterministic_planner",
        **planner_runtime,
        "output_schema_version": "investigation-output-v1",
        "output_schema_coverage_count": sum(row.get("result", {}).get("output_schema_version") == "investigation-output-v1" for row in paired_traces),
        "pending_adjudication_field_count": sum(value.startswith("pending_adjudication:") for row in paired_predictions for value in row.values()),
        "request_count": len(paired_traces),
        "run_success_count": sum(row["status"] == "completed" for row in paired_traces),
        "run_terminal_success_count": sum(row["status"] in {"completed", "needs_clarification"} for row in paired_traces),
        "needs_clarification_count": sum(row["status"] == "needs_clarification" for row in paired_traces),
        "job_success_count": sum(row["job"]["status"] == "completed" for row in paired_traces),
        "completed_count": sum(
            row["status"] == "completed" and row["job"]["status"] == "completed" for row in paired_traces
        ),
        "job_failures": [
            {"scenario_id": row["scenario_id"], "status": row["job"]["status"], "error": row["job"]["error"]}
            for row in paired_traces
            if row["job"]["status"] != "completed"
        ],
        "tool_call_count": planner_call_count,
        **planner_accounting,
        "tool_names": sorted({item["name"] for row in paired_traces for item in row["tools"]}),
        "pairs_with_evidence_or_follow_up_parameter_divergence": sum(
            item["follow_up_query_changed_after_evidence"] or item["retrieved_evidence_changed"]
            for item in pair_divergence
        ),
        "pairs_with_tool_or_parameter_divergence": sum(
            item["tool_names_changed"] or item["executed_search_parameter_changed"]
            for item in pair_divergence
        ),
        "pairs_with_evidence_driven_follow_up_parameter_divergence": sum(
            item["follow_up_parameter_changed_with_evidence"] for item in pair_divergence
        ),
        "pair_count": len(pair_divergence),
        "pair_differences": pair_divergence,
        "scoring": score,
        "predictions": paired_predictions,
        "unsupported_prediction_field_count": sum(
            prediction[field].startswith("unsupported:")
            for prediction in paired_predictions
            for field in paired_predictions[0] if field != "scenario_id"
        ),
        "trajectories": paired_traces,
        "limitation": (
            "Real deterministic traces and structured unknown conclusions are exported. Semantic judgments are pending independent adjudication; exact synthetic contract mismatch is not real-model accuracy."
            if EVAL_PLANNER_PROVIDER == "deterministic"
            else "This is a real configured model-planner run; human adjudication, frozen external quality labels, provider spend review, and model-quality acceptance remain separate gates."
        ),
    }
    binding = bind_observed_execution(execution_config, paired_traces, retrieval_outputs)
    identity = execution_identity(execution_config, paired_traces, retrieval_outputs, runtime_manifest_sha256)
    paired_summary['execution_identity'] = identity
    retrieval_summary['execution_identity'] = identity
    paired_summary['execution_configuration'] = binding
    retrieval_summary['execution_configuration'] = binding
    paired_summary['limitation'] = ('Synthetic corpus, actual project API/worker execution; requested provider is not proof of model dispatch. '
                                     'See execution_configuration for observed provider; independent labels and production approval remain separate.')
    validation_dir = PACK / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)
    report_suffix = os.getenv("INVESTIGATION_EVAL_REPORT_SUFFIX", "")
    assert report_suffix, "Unique report suffix required; frozen reports must not be overwritten"
    assert report_suffix.replace("_", "").replace("-", "").isalnum()
    assert not (validation_dir / f"real_retrieval_runtime_{report_suffix}.json").exists()
    assert not (validation_dir / f"real_paired_investigation_runtime_{report_suffix}.json").exists()
    assert not (validation_dir / f"fixed_planner_comparison_{report_suffix}.json").exists()
    assert not (validation_dir / f"planner_comparison_{report_suffix}.json").exists()
    (validation_dir / f"real_retrieval_runtime_{report_suffix}.json").write_text(
        json.dumps(retrieval_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (validation_dir / f"real_paired_investigation_runtime_{report_suffix}.json").write_text(
        json.dumps(paired_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert len(fixed_traces) == len(paired_traces) == 48
    assert len({row["investigation_id"] for row in fixed_traces}) == 48
    assert len({row["investigation_id"] for row in paired_traces}) == 48
    assert not {row["investigation_id"] for row in fixed_traces}.intersection(
        row["investigation_id"] for row in paired_traces
    )
    assert all(item["budget"]["max_steps"] == 8 for item in [*fixed_traces, *paired_traces])
    assert all(
        item["model_usage"]["tokens"] == 0 and Decimal(item["model_usage"]["cost"]) == 0
        for item in [*fixed_traces, *paired_traces]
    )
    assert all(
        arm["external_model_dispatches"] == 0
        and Decimal(arm["external_model_cost_actual_or_reserved"]) == 0
        and arm["external_model_unknown_receipts"] == 0
        for arm in (fixed_accounting, planner_accounting)
    )
    assert all(
        retrieval.get("phase") not in {"counter_evidence", "model_follow_up"}
        for trace in fixed_traces
        for retrieval in trace["retrievals"]
    )
    fixed_summary = {
        "status": "executed",
        "controller": "fixed-workflow-snapshot-text-v1",
        "controller_source_sha256": hashlib.sha256(inspect.getsource(FixedWorkflowPlanner).encode()).hexdigest(),
        "execution_path": "same Django REST investigation API, source facts, 48 input rows and max_steps=8; test-only fixed controller override",
        "runtime_corpus_version": runtime_manifest["version"],
        "runtime_corpus_manifest_sha256": runtime_manifest_sha256,
        "runtime_pair_inputs_sha256": runtime_manifest["files"]["paired_runtime_inputs.csv"]["sha256"],
        "runtime_documents_sha256": runtime_manifest["files"]["support_cases.csv"]["sha256"],
        "request_count": len(fixed_traces),
        "job_success_count": sum(row["job"]["status"] == "completed" for row in fixed_traces),
        "run_terminal_success_count": sum(row["status"] in {"completed", "needs_clarification"} for row in fixed_traces),
        "tool_call_count": fixed_call_count,
        **fixed_accounting,
        **fixed_runtime,
        "adaptive_counter_evidence_search": "disabled_for_comparison",
        "scoring": fixed_score,
        "predictions": fixed_predictions,
        "trajectories": fixed_traces,
        "limitation": "Previously used synthetic contract scenarios; score status not_established is not independent semantic adjudication or model-quality acceptance.",
    }
    comparison = {
        "schema_version": "planner-comparison-v1",
        "status": "free_arms_executed_real_model_arm_pending_authorization",
        "task_input": {
            "scenario_count": len(paired_inputs),
            "pair_count": len(gold_rows) // 2,
            "runtime_pair_inputs_sha256": runtime_manifest["files"]["paired_runtime_inputs.csv"]["sha256"],
            "runtime_manifest_sha256": runtime_manifest_sha256,
            "runtime_corpus_sha256": runtime_manifest["files"]["support_cases.csv"]["sha256"],
            "same_rows_for_all_arms": True,
            "split_grouping": "pair_id; development/validation/test preserved",
        },
        "common_budget": {
            "max_steps_per_investigation": 8,
            "external_dispatches_allowed": 0,
            "external_cost_allowance": "0",
            "currency": "USD",
        },
        "common_acceptance_rubric": {
            "path": str(gold_path.relative_to(REPO)),
            "sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
            "scorer_sha256": hashlib.sha256((REPO / "dataset/score_paired_investigations.py").read_bytes()).hexdigest(),
            "scope": "synthetic contract replay / development regression only; independent correctness remains not established",
        },
        "arms": {
            "fixed_workflow": {
                "controller": fixed_summary["controller"],
                "controller_source_sha256": fixed_summary["controller_source_sha256"],
                "request_count": fixed_summary["request_count"],
                "job_success_count": fixed_summary["job_success_count"],
                "run_terminal_success_count": fixed_summary["run_terminal_success_count"],
                "tool_call_count": fixed_call_count,
                **fixed_accounting,
                **fixed_runtime,
                "adaptive_counter_evidence_search": "disabled_for_comparison",
                "scoring_status": fixed_score["status"],
                "pairs_passed": fixed_score["pairs_passed"],
                "pairs_not_established": fixed_score["pairs_not_established"],
            },
            "deterministic_planner": {
                "controller": "project DeterministicPlanner",
                "request_count": len(paired_traces),
                "job_success_count": paired_summary["job_success_count"],
                "run_terminal_success_count": paired_summary["run_terminal_success_count"],
                "tool_call_count": planner_call_count,
                **planner_accounting,
                **planner_runtime,
                "adaptive_counter_evidence_search": "enabled_for_production_deterministic_planner",
                "scoring_status": score["status"],
                "pairs_passed": score["pairs_passed"],
                "pairs_not_established": score["pairs_not_established"],
            },
            "real_model_planner": {
                "status": "not_run",
                "reason": "no fresh whole-round model budget/transfer authorization for this comparison",
                "same_input_and_rubric": "reserved by this report schema; requires a new authorized run and fresh output seal",
            },
        },
        "decision": "The free arms are comparable on frozen task rows and budget; synthetic scoring is not independent quality acceptance.",
    }
    fixed_path = validation_dir / f"fixed_planner_comparison_{report_suffix}.json"
    fixed_path.write_text(json.dumps(fixed_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    comparison_path = validation_dir / f"planner_comparison_{report_suffix}.json"
    comparison_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert binding["configuration_execution_status"] == "matched", binding
    assert paired_summary["request_count"] == 48
    assert all(len(row["retrievals"]) <= 4 for row in paired_traces)
    assert paired_summary["run_terminal_success_count"] == 48
    # These unchanged quality thresholds are enforced by dataset/quality_gate.py.
    assert paired_summary["pair_count"] == 24
    assert next(row for row in paired_traces if row["scenario_id"] == "PAIR-19-supports_claim")["status"] == "needs_clarification"
    assert retrieval_summary["overall"]["query_count"] == 30
