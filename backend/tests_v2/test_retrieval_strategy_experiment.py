"""Independent synthetic development comparison, never a blind evaluation.

Candidates are local experiments only: query planning changes, while real DB
ranking, source scope, citation output and API authorization remain in place.
"""
import json
import os
import time
import uuid
from datetime import date
from pathlib import Path

import pytest

from insight.semantic import retrieval
from tests_v2.test_semantic import review_report  # noqa: F401

DOCS = [
    {"id": "PACK", "text": "The packaging was damaged on arrival."},
    {"id": "SAFE", "text": "The device overheated during charging."},
    {"id": "FLOW", "text": "Traffic declined during the weekend."},
    {"id": "CN", "text": "包装受损，到货时外箱已经破裂。", "language": "zh"},
    {"id": "CHARGE", "text": "Slow charging takes many hours."},
]
CASES = [
    ("Please investigate packaging damage with additional context", {"PACK", "CN"}),
    ("Can you find overheating feedback from yesterday", {"SAFE"}),
    ("梳理包装受损的实际反馈", {"PACK", "CN"}),
    ("traffic decline discussion from the weekend", {"FLOW"}),
    ("slow charging customer narrative please", {"CHARGE"}),
    ("packaging damage caused by meteorites", set()),
    ("traffic fell because of alien interference", set()),
    ("violet comet chronology", set()),
    ("packaging intact", set()),
    ("packaging damage on arrival from meteorites", set()),
    ("包装受损", {"PACK", "CN"}),
    ("slow charging", {"CHARGE"}),
    ("overheating", {"SAFE"}),
]


@pytest.mark.django_db
@pytest.mark.parametrize("review_report", [DOCS], indirect=True)
def test_compare_bounded_development_candidates(review_report, monkeypatch):  # noqa: F811
    original = retrieval.query_groups

    def core_only(query):
        groups = original(query)
        core = [group for group in groups if group in retrieval.ALIASES]
        return core or groups

    def core_with_unknown_anchor(query):
        groups = original(query)
        core = [group for group in groups if group in retrieval.ALIASES]
        auxiliary = [group for group in groups if group not in retrieval.ALIASES]
        return core + auxiliary[:1] if core else groups

    users, shops = review_report
    runs = {}
    for name, planner in (("baseline", original), ("core_only", core_only), ("core_plus_first_auxiliary", core_with_unknown_anchor)):
        monkeypatch.setattr(retrieval, "query_groups", planner)
        outputs = []
        for query, relevant in CASES:
            started = time.perf_counter()
            result = retrieval.retrieve(users["a"], shops[0], "B000TEST01", query=query,
                                        start=date(2026, 9, 1), end=date(2026, 9, 2), limit=10)
            found = {item["record_id"] for item in result["items"] if item["match_kind"] == "relevant_hit"}
            outputs.append({"query": query, "relevant": sorted(relevant), "returned": sorted(found),
                            "hits": len(found & relevant), "false_documents": len(found - relevant),
                            "no_answer_false_hit": not relevant and bool(found),
                            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                            "citation_fields_present": all(item.get("fact_id") and item.get("raw_row_id") for item in result["items"])})
        denominator = sum(len(relevant) for _, relevant in CASES)
        runs[name] = {"recall_at_10": sum(row["hits"] for row in outputs) / denominator,
                      "false_documents": sum(row["false_documents"] for row in outputs),
                      "no_answer_false_hits": sum(row["no_answer_false_hit"] for row in outputs),
                      "no_answer_queries": sum(not relevant for _, relevant in CASES), "rows": outputs}
    assert runs["core_only"]["recall_at_10"] > runs["baseline"]["recall_at_10"]
    assert runs["core_only"]["no_answer_false_hits"] > 0
    assert runs["core_plus_first_auxiliary"]["no_answer_false_hits"] > 0
    report = {"dataset": "synthetic-independent-development-20261007", "blind_test": False,
              "execution": "actual retrieve() PostgreSQL queries on isolated published fixture",
              "baseline_version": retrieval.RETRIEVAL_VERSION, "strategies": runs,
              "selected_for_production": "baseline", "candidate_status": "rejected",
              "reason": "Relaxed unknown constraints introduce unsupported causal/property matches; no frozen query was used to select aliases.",
              "next_capability": "Separate semantic intent constraints from auxiliary wording, with independently labeled no-answer entailment evaluation before promotion."}
    suffix = os.getenv("INVESTIGATION_EVAL_REPORT_SUFFIX") or uuid.uuid4().hex
    assert suffix.replace("_", "").replace("-", "").isalnum()
    path = Path(__file__).resolve().parents[2] / f".local/acceptance-artifacts/development-retrieval/retrieval_strategy_experiment_{suffix}.json"
    assert not path.exists(), "Use a unique report suffix; never overwrite historical development evidence"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
