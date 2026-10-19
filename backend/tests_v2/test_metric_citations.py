import copy
import json

import pytest

from app.agent.output_contract import FINAL_SCHEMA_VERSION
from app.agent.planner import LLMPlanner
from insight.investigation import planner
from insight.investigation.business_schema import BUSINESS_FIELDS, BUSINESS_SCHEMA_VERSION
from insight.investigation.citations import metric_catalog, validate_citations
from insight.investigation.output import compose_output
from insight.metrics.facts import source_manifest
from insight.models import InvestigationRun, MetricSnapshot
from insight.tasks import run_job

METRICS = {"daily": {"status": "ready", "baseline": {"start": "2026-08-01", "end": "2026-08-28", "sessions": 800, "currency": "USD", "sales_amount": "50"},
                     "current": {"start": "2026-08-29", "end": "2026-09-04", "sessions": 200, "unit_session_percent": "4.0", "currency": "USD", "sales_amount": "20"}},
           "mapping_versions": {}}


def envelope():
    catalog = metric_catalog(snapshot_id="snapshot-1", snapshot_fingerprint="fingerprint-1", source_manifest={"s1": {"revision": 1}},
                             shop_id="shop-1", child_asin="B000TEST01", product_id=None, metrics=METRICS)
    return {"type": "metric_snapshot", "metric_evidence": catalog, "metrics": METRICS}


@pytest.mark.parametrize("field,value", [("value", "9"), ("unit", "EUR"), ("denominator", None), ("start", "2026-09-01"),
                                       ("window", "baseline"), ("shop_id", "other-shop"), ("child_asin", "B000OTHER1"),
                                       ("source_manifest_sha256", "new-source"), ("snapshot_id", "other-snapshot"), ("snapshot_fingerprint", "old")])
def test_metric_citation_must_match_every_frozen_field(field, value):
    evidence = envelope()
    atom = next(item for item in evidence["metric_evidence"]["atoms"] if item["path"] == "daily.current.unit_session_percent")
    assert validate_citations([atom], [evidence]) == [atom]
    bad = {**atom, field: value}
    with pytest.raises(ValueError):
        validate_citations([bad], [evidence])


def test_only_metrics_can_support_a_typed_fact_without_unrelated_text():
    from app.agent.output_contract import validate_final_answer
    evidence = envelope()
    atom = next(item for item in evidence["metric_evidence"]["atoms"] if item["path"] == "daily.current.unit_session_percent")
    answer = {"schema_version": FINAL_SCHEMA_VERSION, "statement": "Frozen ratio is 4.0 percent with 200 sessions.", "citations": [atom],
              "business_contract": {"schema_version": BUSINESS_SCHEMA_VERSION, **{key: "unknown" for key in BUSINESS_FIELDS}, "allowed_conclusion": "cited_metric_fact"}}
    validate_final_answer(answer, [{"data": evidence}])
    result = compose_output([evidence], [{"conclusion": answer}])
    assert result["conclusion"]["citation_status"] == "verified_metric_facts"
    assert result["conclusion"]["status"] == "pending_adjudication"
    answer["business_contract"]["allowed_conclusion"] = "reviews_caused_conversion_loss"
    with pytest.raises(ValueError, match="not causal"):
        validate_final_answer(answer, [{"data": evidence}])


@pytest.mark.django_db(transaction=True)
def test_real_api_metric_only_production_gateway_and_final_db_recheck(metric_scope, client, monkeypatch):
    users, shops = metric_scope
    snapshot = MetricSnapshot.objects.create(shop=shops[0], child_asin="B000TEST01", actor=users["a"],
                                            fingerprint="typed-metric-controlled", rule_version="controlled", source_manifest=source_manifest(shops[0]),
                                            payload=copy.deepcopy(METRICS), local_date="2026-09-05")

    class Gateway:
        last_usage = {}
        provenance = "controlled-metric-gateway"

        def plan(self, *, system, user, schema):
            request = json.loads(user)
            payload = {"decision": "call_tool", "tool_name": "metric_snapshot", "arguments": "{}", "decision_summary": "Read frozen metric",
                       "target_hypothesis_id": None, "stop_reason": None, "final_answer": None, "confidence": .5,
                       "missing_fields": "", "counter_evidence_query": ""}
            metrics = [item["data"] for item in request["observations"] if item["data"].get("metric_evidence")]
            if metrics:
                atom = next(item for item in metrics[0]["metric_evidence"]["atoms"] if item["path"] == "daily.current.unit_session_percent")
                payload.update(decision="stop", tool_name=None, stop_reason="frozen_fact_only", final_answer={
                    "schema_version": FINAL_SCHEMA_VERSION, "statement": "Frozen ratio: 4.0 percent, denominator 200 sessions.", "citations": [atom],
                    "business_contract": {"schema_version": BUSINESS_SCHEMA_VERSION, **{key: "unknown" for key in BUSINESS_FIELDS}, "allowed_conclusion": "cited_metric_fact"}})
            return json.dumps(payload)

    model = planner.ModelPlanner.__new__(planner.ModelPlanner)
    model.planner = LLMPlanner(Gateway())
    monkeypatch.setattr(planner, "planner_for", lambda _provider: model)
    client.force_authenticate(users["a"])
    response = client.post("/api/v1/investigations/", {"shop": str(shops[0].pk), "child_asin": "B000TEST01", "query": "查看两窗转化指标事实",
                                                   "max_steps": 8, "idempotency_key": "metric-protocol"}, format="json")
    assert response.status_code == 202
    run_job(response.data["id"])
    run = InvestigationRun.objects.get(pk=response.data["result"]["investigation_id"])
    assert run.status == "completed", run.stop_reason
    assert run.result["conclusion"]["citation_status"] == "verified_metric_facts"
    assert run.result["observations"] == []
    assert run.result["metric_evidence"][0]["identity"]["snapshot_id"] == str(snapshot.pk)
    from rest_framework.exceptions import ValidationError

    from insight.investigation.citations import recheck_metric_evidence
    evidence = run.result["evidence"]
    recheck_metric_evidence(run, evidence)
    snapshot.payload["daily"]["current"]["unit_session_percent"] = "99"
    snapshot.save(update_fields=["payload"])
    with pytest.raises(ValidationError):
        recheck_metric_evidence(run, evidence)


@pytest.mark.django_db
@pytest.mark.parametrize("mutation", ["revoked", "rule_version", "mapping_version", "source_version", "cross_scope", "frozen_id"])
def test_final_db_recheck_rejects_changed_authority_and_versions(metric_scope, mutation):
    from rest_framework.exceptions import APIException

    from insight.investigation.citations import recheck_metric_evidence
    from insight.models import ShopGrant
    users, shops = metric_scope
    manifest = source_manifest(shops[0])
    snapshot = MetricSnapshot.objects.create(shop=shops[0], child_asin="B000TEST01", actor=users["a"], fingerprint="authority-check",
                                            rule_version="controlled", source_manifest=manifest, payload=copy.deepcopy(METRICS), local_date="2026-09-05")
    run = InvestigationRun.objects.create(shop=shops[0], requested_by=users["a"], child_asin="B000TEST01", query="frozen metrics",
                                          input_versions={"metric_snapshot": str(snapshot.pk), "source_manifest": manifest, "rule_version": "controlled", "mapping_versions": {}})
    snapshot.refresh_from_db()  # Use the persisted JSONB order, as the production tool does.
    evidence = [{"type": "metric_snapshot", "metric_evidence": metric_catalog(snapshot_id=snapshot.pk, snapshot_fingerprint=snapshot.fingerprint,
                 source_manifest=manifest, shop_id=shops[0].pk, child_asin="B000TEST01", product_id=None, metrics=snapshot.payload)}]
    recheck_metric_evidence(run, evidence)
    if mutation == "revoked":
        ShopGrant.objects.filter(user=users["a"], shop=shops[0]).delete()
    elif mutation == "rule_version":
        snapshot.rule_version = "new-rule"
        snapshot.save(update_fields=["rule_version"])
    elif mutation == "mapping_version":
        snapshot.payload["mapping_versions"] = {"changed": 2}
        snapshot.save(update_fields=["payload"])
    elif mutation == "source_version":
        snapshot.source_manifest = {"changed": {"version": 2}}
        snapshot.save(update_fields=["source_manifest"])
    elif mutation == "cross_scope":
        run.shop = shops[1]
        run.save(update_fields=["shop"])
    else:
        run.input_versions["metric_snapshot"] = "unbound-snapshot"
        run.save(update_fields=["input_versions"])
    with pytest.raises(APIException):
        recheck_metric_evidence(run, evidence)


def test_title_citation_requires_explicit_field_and_preserves_legacy_body():
    evidence = [{"type": "text_retrieval", "items": [{"fact_id": "f1", "match_kind": "relevant_hit", "title": "Overheating", "text": "Arrived yesterday."}]}]
    title = {"fact_id": "f1", "quote": "Overheating", "source_field": "title"}
    assert validate_citations([title], evidence) == [title]
    body = {"fact_id": "f1", "quote": "Arrived yesterday."}
    assert validate_citations([body], evidence) == [body]
    for bad in ({"fact_id": "f1", "quote": "Overheating"}, {**title, "source_field": "text"},
                {**title, "quote": "OverheatingArrived yesterday."}, {**title, "source_field": "raw_row"}):
        with pytest.raises(ValueError, match="invalid original citation"):
            validate_citations([bad], evidence)
    evidence[0]["items"][0]["match_kind"] = "scope_context"
    with pytest.raises(ValueError):
        validate_citations([title], evidence)
