"""Independent development cases; controlled vectors do not certify semantics."""
# ruff: noqa: F811
import json
import os
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from insight.metrics.facts import latest_facts, source_manifest
from insight.semantic import relevance, retrieval
from insight.semantic.vector_candidates import (
    LocalEmbedding,
    build_index,
    fuse_candidates,
    index_scope,
    vector_candidates,
)
from tests_v2.test_quality_candidate_stage import RejectGateway
from tests_v2.test_semantic import review_report  # noqa: F401


class ControlledVectors:
    dimension = 2
    identity = {"model": "controlled-vector-fixture", "dimension": 2, "semantic_quality": "not_established"}

    def embed(self, text, *, query=False):
        return np.array([1, 0] if query or "carton" in text else [0, 1], dtype=np.float32)


def test_fusion_reserves_semantic_slots_and_deduplicates():
    selected, _ = fuse_candidates([str(n) for n in range(200)], ["novel", "other", "0"], 4)
    assert len(selected) == len(set(selected)) == 4
    assert {"novel", "other"} <= set(selected)


@pytest.mark.parametrize("url", ["https://example.com/v1", "http://localhost:1234/v1", "http://127.0.0.1:1234/v1?secret=x"])
def test_embedding_disallows_remote_and_ambiguous_urls(monkeypatch, url):
    monkeypatch.setenv("EVIDENCE_LOCAL_EMBEDDING_URL", url)
    with pytest.raises(ValueError, match="literal_loopback"):
        LocalEmbedding()


DOCS = [{"id": "NEW", "text": "The carton arrived crushed and the insert was torn."}] + [
    {"id": f"DISTRACTOR-{n}", "text": f"The user manual has {n + 5} pages."} for n in range(35)]


@pytest.mark.django_db
@pytest.mark.parametrize("review_report", [DOCS], indirect=True)
def test_hybrid_intersects_authorized_ids_and_admits_synonym_candidate_only(review_report, monkeypatch, tmp_path, enable_workspace_model_budget):
    users, shops = review_report
    enable_workspace_model_budget(shops[0].workspace, users["admin"])
    manifest = source_manifest(shops[0])
    rows = latest_facts(manifest, "reviews", "B000TEST01", None)
    scope = index_scope(shops[0], "B000TEST01", None, "reviews", manifest)
    provider = ControlledVectors()
    monkeypatch.setenv("EVIDENCE_VECTOR_INDEX_DIR", str(tmp_path))
    build_index(rows, scope, start=date(2026, 9, 1), end=date(2026, 9, 2), provider=provider)
    narrowed = rows.filter(record_id="NEW")
    ids, meta = vector_candidates(narrowed, scope, query="packaging damage", start=date(2026, 9, 1), end=date(2026, 9, 2), provider=provider)
    assert ids == [str(narrowed.get().pk)] and meta["indexed_authorized_count"] == 1
    import insight.semantic.vector_candidates as module
    monkeypatch.setattr(module, "LocalEmbedding", lambda: provider)
    monkeypatch.setenv("EVIDENCE_RELEVANCE_ENABLED", "1")
    monkeypatch.setenv("EVIDENCE_CANDIDATE_MODE", "hybrid_local")
    monkeypatch.setenv("EVIDENCE_RELEVANCE_MAX_CANDIDATES", "4")
    monkeypatch.setattr(relevance, "configured_adapter", lambda: relevance.RelevanceAdapter(RejectGateway(), max_candidates=4, input_rate="1", output_rate="1"))
    result = retrieval.retrieve(users["a"], shops[0], "B000TEST01", query="packaging damage", start=date(2026, 9, 1), end=date(2026, 9, 2))
    stage = result["candidate_stage"]
    assert stage["scoped_count"] > 20 and stage["lexical_matched_count"] == 0
    assert stage["adjudication_input_count"] == 4
    assert str(narrowed.get().pk) in stage["semantic_candidates"]["fused_ids"]
    assert result["items"] == [] and result["retrieval_complete"] is False
    assert result["match_status"] == "incomplete"  # Bounded no-hit cannot prove absence.
    rows.filter(record_id="NEW").update(text="Changed original")
    with pytest.raises(ValueError, match="original_mismatch"):
        vector_candidates(rows, scope, query="packaging damage", start=date(2026, 9, 1), end=date(2026, 9, 2), provider=provider)


@pytest.mark.django_db
@pytest.mark.parametrize("review_report", [DOCS], indirect=True)
def test_missing_hybrid_index_is_incomplete_not_lexical_fallback(review_report, monkeypatch, tmp_path):
    users, shops = review_report
    import insight.semantic.vector_candidates as module
    monkeypatch.setattr(module, "LocalEmbedding", ControlledVectors)
    monkeypatch.setenv("EVIDENCE_RELEVANCE_ENABLED", "1")
    monkeypatch.setenv("EVIDENCE_CANDIDATE_MODE", "hybrid_local")
    monkeypatch.setenv("EVIDENCE_VECTOR_INDEX_DIR", str(tmp_path))
    monkeypatch.setattr(relevance, "configured_adapter", lambda: relevance.RelevanceAdapter(RejectGateway(), input_rate="1", output_rate="1"))
    result = retrieval.retrieve(users["a"], shops[0], "B000TEST01", query="manual", start=date(2026, 9, 1), end=date(2026, 9, 2))
    assert result["candidate_stage"]["semantic_candidates"]["error"] == "embedding_index_missing"
    assert not result["retrieval_complete"] and not result["items"]


DEVELOPMENT_DOCS = [
    {"id": "DEV-001", "text": "The carton arrived crushed and its protective insert was torn."},
    {"id": "DEV-002", "text": "The connector kept disconnecting unless the cable was held firmly."},
    {"id": "DEV-003", "text": "Within minutes the unit became too warm to touch and emitted smoke."},
    {"id": "DEV-004", "text": "After plugging it in overnight, the battery indicator had barely moved."},
    {"id": "DEV-005", "text": "The parcel was unblemished, with an intact carton and protective insert."},
    {"id": "DEV-006", "text": "If the enclosure emitted fumes in the future, I would unplug it."},
    {"id": "DEV-007", "text": "The enclosure never emitted smoke and remained cool throughout use."},
] + [{"id": f"DEV-{n + 10:03}", "text": f"The printed instruction booklet lists {n + 8} pages. Delivery was prompt."} for n in range(35)]
DEVELOPMENT_QUERIES = [
    ("packaging damage", {"DEV-001"}),
    ("unstable electrical contact", {"DEV-002"}),
    ("actual overheating incident", {"DEV-003"}),
    ("very slow charging", {"DEV-004"}),
    ("parcel arrived intact without damage", {"DEV-005"}),
    ("hypothetical fumes", {"DEV-006"}),
    ("no smoke during operation", {"DEV-007"}),
]


@pytest.mark.skipif(not os.getenv("QUALITY_LOCAL_EMBEDDING_RUN"), reason="explicit existing local model required")
@pytest.mark.django_db
@pytest.mark.parametrize("review_report", [DEVELOPMENT_DOCS], indirect=True)
def test_actual_existing_local_embeddings_on_independent_development(review_report, monkeypatch):
    users, shops = review_report
    output = Path(os.environ["QUALITY_LOCAL_EMBEDDING_RUN"])
    config = json.loads((output / "local-embedding.json").read_text())
    monkeypatch.setenv("EVIDENCE_LOCAL_EMBEDDING_MODEL", config["model"])
    monkeypatch.setenv("EVIDENCE_LOCAL_EMBEDDING_SHA256", config["model_sha256"])
    monkeypatch.setenv("EVIDENCE_VECTOR_INDEX_DIR", str(output / "development-index"))
    rows = latest_facts(source_manifest(shops[0]), "reviews", "B000TEST01", None)
    scope = index_scope(shops[0], "B000TEST01", None, "reviews", source_manifest(shops[0]))
    import time
    started = time.perf_counter()
    build = build_index(rows, scope, start=date(2026, 9, 1), end=date(2026, 9, 2))
    build["seconds"] = time.perf_counter() - started
    outputs = []
    for query, positives in DEVELOPMENT_QUERIES:
        started = time.perf_counter()
        semantic, meta = vector_candidates(rows, scope, query=query, start=date(2026, 9, 1), end=date(2026, 9, 2))
        groups = retrieval.query_groups(query)
        predicate = None
        for group in groups:
            part = retrieval.group_filter(group, ["title", "text"])
            predicate = part if predicate is None else predicate | part
        lexical_rows = rows.filter(predicate) if predicate is not None else rows.none()
        lexical = [str(row.pk) for row in lexical_rows.order_by("id")[:200]]
        chosen, _ = fuse_candidates(lexical, semantic, 20)
        record_ids = {str(row.pk): row.record_id for row in rows}
        outputs.append({"query": query, "semantic_at_10": [record_ids[pk] for pk in semantic[:10]],
                        "fused_candidates": [record_ids[pk] for pk in chosen], "lexical_candidates": [record_ids[pk] for pk in lexical],
                        "development_positives": sorted(positives), "candidate_hits": len(positives & {record_ids[pk] for pk in chosen}),
                        "lexical_hits": len(positives & {record_ids[pk] for pk in lexical}),
                        "seconds": time.perf_counter() - started, "metadata": meta})
    report = {"version": "independent-synthetic-development-candidates-v1", "scope_documents": rows.count(),
              "build": build, "queries": outputs, "candidate_recall_at_20": sum(row["candidate_hits"] for row in outputs) / len(outputs),
              "lexical_candidate_recall": sum(row["lexical_hits"] for row in outputs) / len(outputs),
              "final_recall": "not_run", "negation_entailment": "not_run", "semantic_quality_accepted": False,
              "external_calls": 0, "frozen_test_read": False}
    with (output / "local-development-candidates.json").open("x") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    assert all(len(row["fused_candidates"]) <= 20 for row in outputs)
