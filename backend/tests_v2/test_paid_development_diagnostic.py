"""Explicit opt-in synthetic development diagnostic; never part of normal CI."""
import csv
import hashlib
import io
import json
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.skipif(
    not os.getenv("QUALITY_DEVELOPMENT_RUN"), reason="explicit isolated diagnostic required")]


def write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def test_controlled_development_graph(setup_scope, settings, monkeypatch):
    import openai
    from django.core.files.uploadedfile import SimpleUploadedFile
    from rest_framework.test import APIClient

    from insight.ingestion.service import accept_upload, create_source, parse_batch, publish_batch
    from insight.investigation import model_limits
    from insight.models import InvestigationRun, Job, Product, ProductMapping, Shop, ShopGrant
    from insight.semantic.retrieval import retrieve
    from insight.tasks import run_job

    run_dir = Path(os.environ["QUALITY_DEVELOPMENT_RUN"]).resolve()
    paid = os.getenv("QUALITY_DEVELOPMENT_PAID") == "1"
    capture_name = os.getenv("QUALITY_DEVELOPMENT_CAPTURE_NAME", "offline")
    assert capture_name in {"offline", "candidate-offline"}
    mode = "paid" if paid else capture_name
    destination = run_dir / mode
    destination.mkdir()  # Never redispatch an existing diagnostic.
    inputs = json.loads((run_dir / "inputs.json").read_text())
    approval = json.loads((run_dir / "approval.json").read_text())
    assert approval["hard_cap_usd"] == "0.11"
    if paid:
        assert approval["approved"]
        for name in ("plan.json", "runtime-source.json", "diagnostic.env"):
            assert hashlib.sha256((run_dir / name).read_bytes()).hexdigest() == approval["binding_sha256"][name]
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "nonfunctional-offline-placeholder")
    assert hashlib.sha256((run_dir / "inputs.json").read_bytes()).hexdigest() == approval["inputs_sha256"]
    repository = Path(__file__).resolve().parents[2]
    if paid:
        offline = run_dir / "offline"
        for name, digest in json.loads((offline / "seal.json").read_text()).items():
            assert hashlib.sha256((offline / name).read_bytes()).hexdigest() == digest
        prior = json.loads((offline / "summary.json").read_text())
        assert prior["real_calls"] == 0 and all(not item["job_error"] for item in prior["outputs"])
        for name, digest in json.loads((run_dir / "runtime-source.json").read_text()).items():
            assert hashlib.sha256((repository / name).read_bytes()).hexdigest() == digest

        def read_boundary(event, arguments):
            if event != "open" or not isinstance(arguments[0], (str, bytes, os.PathLike)):
                return
            path = Path(os.fsdecode(arguments[0])).resolve()
            forbidden = [repository / "dataset", repository / "docs", repository / ".local/acceptance-artifacts"]
            if any(path.is_relative_to(prefix) for prefix in forbidden) and not path.is_relative_to(run_dir):
                raise PermissionError("development inference cannot read labels, scoring data or prior runs")

        sys.addaudithook(read_boundary)
    settings.STORAGE_ROOT = destination / "storage"
    users, shops = setup_scope
    opening = json.loads((run_dir / "budget-opening.json").read_text()) if (run_dir / "budget-opening.json").exists() else []
    if paid and opening:
        assert hashlib.sha256((run_dir / "budget-opening.json").read_bytes()).hexdigest() == approval["binding_sha256"]["budget-opening.json"]
    calls = []
    reserved = sum((Decimal(entry["reserved_usd"]) for entry in opening), Decimal("0"))
    group_counts = {}
    for entry in opening:
        group_counts[entry["group"]] = group_counts.get(entry["group"], 0) + 1
    group = ""
    real_constructor = openai.OpenAI
    real_limits = model_limits.planner_request_limits
    limit_calls = []
    stopped = {}

    def capture_limits(system, user, schema):
        limit_calls.append({"group": group, "bytes": len((system + user + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))).encode()) + 1024})
        write(destination / f"planner-input-{len(limit_calls):02}.json", {"system": system, "user": json.loads(user), "schema": schema, **limit_calls[-1]})
        return real_limits(system, user, schema)

    monkeypatch.setattr(model_limits, "planner_request_limits", capture_limits)

    def constructor(**kwargs):
        assert kwargs["max_retries"] == 0
        client = real_constructor(**kwargs) if paid else None

        def create(**payload):
            nonlocal reserved
            if stopped:
                raise RuntimeError("diagnostic_circuit_open_no_more_external_dispatch")
            name = payload["text"]["format"]["name"]
            planner = name == "agent_planner"
            cap = Decimal("0.0060576" if planner else "0.0029760")
            byte_limit = 8192 if planner else 5120
            wire = json.dumps(payload, ensure_ascii=False).encode()
            assert len(wire) <= byte_limit, "actual SDK envelope exceeds approved bound"
            assert payload["model"] == "deepseek-flash"
            assert payload["reasoning"] == {"effort": "none"}
            assert payload["max_output_tokens"] == (3000 if planner else 1200)
            count = group_counts.get(group, 0)
            assert count < (4 if group.startswith("investigation:") else 1)
            assert len(calls) + len(opening) < 20 and reserved + cap <= Decimal("0.11")
            assert hashlib.sha256(wire).hexdigest() not in {entry["request_sha256"] for entry in opening}, "no repeat of previously dispatched request"
            reserved += cap
            group_counts[group] = count + 1
            receipt = {"group": group, "kind": "planner" if planner else "relevance",
                       "reserved_usd": str(cap), "request_bytes": len(wire),
                       "request_sha256": hashlib.sha256(wire).hexdigest(),
                       "dispatch": paid, "outcome": "unknown" if paid else "offline_substitute"}
            index = len(calls) + 1
            calls.append(receipt)
            write(destination / f"request-{index:02}.json", payload)
            write(destination / f"reservation-{index:02}.json", receipt)
            if paid:
                try:
                    response = client.responses.create(**payload)
                except Exception as exc:
                    if getattr(exc, "status_code", None) == 400:
                        stopped.update(reason="HTTP400", request=index, group=group)
                    write(destination / f"error-{index:02}.json", {"type": type(exc).__name__, "reservation_retained": True,
                          "http_status": getattr(exc, "status_code", None), "request_id": getattr(exc, "request_id", None),
                          "provider_error": getattr(exc, "body", None)})
                    raise
                write(destination / f"response-{index:02}.json", response.model_dump(mode="json"))
                return response
            prompt = json.loads(payload["input"][1]["content"])
            if planner:
                tools = prompt["available_tools"]
                decision = {"decision": "call_tool" if "text_evidence" in tools else "clarify",
                            "tool_name": "text_evidence" if "text_evidence" in tools else None,
                            "arguments": "{}", "decision_summary": "Offline transport fixture; no model inference",
                            "target_hypothesis_id": None, "stop_reason": "offline capture", "final_answer": None,
                            "confidence": None, "missing_fields": "independent judgement", "counter_evidence_query": ""}
                if not tools:
                    from app.agent.output_contract import FINAL_SCHEMA_VERSION
                    from insight.investigation.business_schema import BUSINESS_FIELDS, BUSINESS_SCHEMA_VERSION
                    decision.update(decision="stop", missing_fields="", final_answer={
                        "schema_version": FINAL_SCHEMA_VERSION, "statement": "Offline capture only; no semantic conclusion",
                        "citations": [], "business_contract": {"schema_version": BUSINESS_SCHEMA_VERSION,
                            **{field: "unknown" for field in BUSINESS_FIELDS}}})
            else:
                decision = {"request_sha256": prompt["request_sha256"], "decisions": [
                    {"candidate_id": item["candidate_id"], "label": "relevant", "source_field": "text",
                     "start": 0, "end": len(item["text"]), "quote": item["text"], "constraints": {
                         key: "satisfied" for key in prompt["constraints"]}} for item in prompt["candidates"]]}
            return SimpleNamespace(status="completed", id=f"offline-{index}", model=payload["model"],
                                   output_text=json.dumps(decision), usage=SimpleNamespace(input_tokens=20, output_tokens=20))

        return SimpleNamespace(responses=SimpleNamespace(create=create))

    monkeypatch.setattr(openai, "OpenAI", constructor)
    outputs = []
    for index, case in enumerate(inputs["cases"]):
        shop = Shop.objects.create(workspace=shops[0].workspace, name=f"Synthetic development {index}",
                                   marketplace="US", timezone="America/Los_Angeles", currency="USD")
        ShopGrant.objects.create(user=users["a"], shop=shop, roles=["operator"])
        for asin in {case["asin"], *(doc["asin"] for doc in case["documents"])}:
            product = Product.objects.create(shop=shop, child_asin=asin, seller_sku=f"DEV-{asin}", fulfillment="FBA")
            ProductMapping.objects.create(product=product, version=1, name="Synthetic development product",
                                          effective_from=date(2026, 1, 1), reason="isolated synthetic diagnostic")
        fields = ["child_asin", "seller_sku", "fulfillment", "business_date", "record_id", "text", "status"]
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for doc in case["documents"]:
            writer.writerow(dict(child_asin=doc["asin"], seller_sku=f"DEV-{doc['asin']}", fulfillment="FBA",
                                 business_date=doc["date"], record_id=doc["record_id"], text=doc["text"], status="open"))
        source = create_source(users["admin"], shop, dict(name="Synthetic support", kind="support", is_primary=True,
            grain="product_record", column_mapping={field: field for field in fields}, ignored_columns=[], coverage_basis="business_date"), "development")
        batch = accept_upload(users["admin"], source, SimpleUploadedFile("synthetic.csv", stream.getvalue().encode()),
            dict(coverage_start=date.fromisoformat(case["start"]), coverage_end=date.fromisoformat(case["end"]),
                 generated_at=datetime(2026, 10, 3, tzinfo=UTC), declared_complete=False,
                 declared_asins=list({doc["asin"] for doc in case["documents"]})), "development")
        parse_batch(batch.pk, users["admin"])
        batch.refresh_from_db()
        assert batch.status == "preview" and batch.error_rows == 0
        publish_batch(users["admin"], batch.pk, allow_partial=False, confirm_contract=True, request_id="development")
        client = APIClient()
        client.force_authenticate(users["a"])
        group = f"investigation:{index}"
        response = client.post("/api/v1/investigations/", dict(shop=str(shop.pk), query=case["query"],
            child_asin=case["asin"], max_steps=4, planner_provider="deepseek", allow_internal_task=False,
            idempotency_key=f"development-{run_dir.name}-{index}"), format="json")
        assert response.status_code == 202, response.data
        investigation = InvestigationRun.objects.get(pk=response.data["result"]["investigation_id"])
        investigation.state.update(scope_query=f"{case['start']} 至 {case['end']}")
        investigation.save(update_fields=["state"])
        run_job(response.data["id"])
        investigation.refresh_from_db()
        job = Job.objects.get(pk=response.data["id"])
        trajectory = dict(case_id=case["case_id"], status=investigation.status, state=investigation.state,
            result=investigation.result, decisions=investigation.decisions, stop_reason=investigation.stop_reason,
            job_status=job.status, job_error=job.error,
            steps=list(investigation.steps.values()), ledger=list(investigation.budget_ledger.values()),
            tools=list(investigation.tool_calls.order_by("sequence").values()))
        write(destination / f"trajectory-{index}.json", trajectory)
        if stopped:
            outputs.append({"case_id": case["case_id"], "status": investigation.status, "job_error": job.error})
            break
        group = f"retrieval:{index}"
        result = retrieve(users["a"], shop, case["asin"], query=case["query"],
                          start=date.fromisoformat(case["start"]), end=date.fromisoformat(case["end"]), kind="support")
        write(destination / f"retrieval-{index}.json", result)
        outputs.append({"case_id": case["case_id"], "status": investigation.status, "job_error": job.error})
    write(destination / "summary.json", dict(mode=mode, real_calls=len(calls) if paid else 0,
        captured_calls=len(calls), reserved_usd=str(reserved), groups=group_counts, outputs=outputs,
        inherited_call_units=len(opening), cumulative_call_units=len(opening) + len(calls),
        circuit_stop=stopped, planned_cases=4, executed_cases=len(outputs),
        quality_claim=False, independent_adjudication="not_supplied"))
    refs = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in destination.glob("*.json")}
    write(destination / "seal.json", refs)
    if not paid:
        assert all(not output["job_error"] and output["status"] == "completed" for output in outputs), "offline full-graph prerequisite failed"
