#!/usr/bin/env python3
"""Create a retrieval corpus with query/qrel labels removed from searchable rows."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PACK = ROOT / "data" / "07_S4_S6独立评测_v1"
DEFAULT_OUTPUT = DEFAULT_PACK / "runtime_corpus_v4"
RETRIEVAL_SCOPE_ASIN = "D5X2600001"
DOC_FIELDS = ("child_asin", "seller_sku", "fulfillment", "business_date", "record_id", "text", "status")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def corpus_rows(pack_dir: Path) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    queries = read_csv(pack_dir / "retrieval_queries.csv")
    documents = read_csv(pack_dir / "retrieval_documents.csv")
    paired = read_csv(pack_dir / "paired_investigation_scenarios.csv")
    context_path = pack_dir / "runtime_fixture_context_v4.csv"
    runtime_context = read_csv(context_path) if context_path.exists() else []
    context_by_scenario = {row["scenario_id"]: row for row in runtime_context}
    if len(context_by_scenario) != len(runtime_context):
        raise ValueError("runtime_fixture_context_v4.csv contains duplicate scenario_id values")
    unknown_context_ids = set(context_by_scenario) - {row["scenario_id"] for row in paired}
    if unknown_context_ids:
        raise ValueError(f"Runtime fixture context references unknown scenarios: {sorted(unknown_context_ids)}")
    query_topics = {item["query_id"]: item["topic"] for item in queries}
    query_splits = {item["query_id"]: item["split"] for item in queries}
    safe_documents = []
    for item in documents:
        topic = query_topics[item["query_id"]]
        title = "Synthetic feedback record"
        text = item["text"]
        text = re.sub(r"query\s+topic\s*=\s*[^;,.]+[;,]?\s*", "", text, flags=re.IGNORECASE)
        text = text.replace(topic, "")
        text = re.sub(r"\bno claim for\s*[.;,]?", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\bfixture evidence\s+\d+\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip(" ;,.")
        safe_documents.append(
            {
                # These 120 documents are one scope-wide corpus. Individual
                # source fixture ASINs were assigned per query during dataset
                # construction and must not accidentally partition runtime
                # retrieval into twelve-document slices.
                "child_asin": RETRIEVAL_SCOPE_ASIN,
                "seller_sku": f"EVAL-{RETRIEVAL_SCOPE_ASIN}",
                "fulfillment": "FBA",
                "business_date": item["business_date"],
                "record_id": item["doc_id"],
                "text": f"{title}. {text}",
                "status": "closed",
            }
        )

    paired_inputs = []
    paired_documents = []
    for item in paired:
        context = context_by_scenario.get(item["scenario_id"], {})
        evidence = context.get("runtime_evidence_text", "").strip() or item["evidence_text"].strip()
        evidence_hash = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
        evidence_asin = "B" + evidence_hash[:9].upper()
        # The selected scope is an explicit runtime input. It is not inferred by
        # the app from scenario_id or from a gold answer. "omit" exercises the
        # real locator with no selected product; "evidence_asin" is the normal
        # scoped request.
        scope_selection = context.get("scope_selection", "evidence_asin")
        if scope_selection not in {"omit", "evidence_asin"}:
            raise ValueError(f"Unsupported scope_selection for {item['scenario_id']}: {scope_selection}")
        selected_asin = "" if scope_selection == "omit" else evidence_asin
        paired_inputs.append(
            {
                "scenario_id": item["scenario_id"],
                "query": item["query"],
                "selected_child_asin": selected_asin,
                "evidence_child_asin": evidence_asin,
                "split": item["split"],
            }
        )
        paired_documents.append(
            {
                "child_asin": evidence_asin,
                "seller_sku": f"EVAL-{evidence_asin}",
                "fulfillment": "FBA",
                "business_date": "2026-09-30",
                "record_id": f"PAIR-{evidence_hash[:16].upper()}",
                "text": evidence,
                "status": "closed",
            }
        )

    safe_queries = [
        {
            "query_id": item["query_id"],
            "query": item["query"],
            "language": item["language"],
            "split": item["split"],
        }
        for item in queries
    ]
    return safe_documents + paired_documents, safe_queries, paired_inputs


def build(pack_dir: Path, output_dir: Path) -> dict:
    documents, queries, paired_inputs = corpus_rows(pack_dir)
    context_path = pack_dir / "runtime_fixture_context_v4.csv"
    retrieval_docs = len(read_csv(pack_dir / "retrieval_documents.csv"))
    support_path = output_dir / "support_cases.csv"
    queries_path = output_dir / "retrieval_runtime_queries.csv"
    paired_path = output_dir / "paired_runtime_inputs.csv"
    write_csv(support_path, documents, DOC_FIELDS)
    write_csv(queries_path, queries, ("query_id", "query", "language", "split"))
    write_csv(
        paired_path,
        paired_inputs,
        ("scenario_id", "query", "selected_child_asin", "evidence_child_asin", "split"),
    )
    context_hash = sha256(context_path) if context_path.exists() else None
    manifest = {
        "version": "s4-s6-runtime-corpus-v4",
        "generated_at": date.today().isoformat(),
        "source_pack": pack_dir.name,
        "document_count": len(documents),
        "retrieval_document_count": retrieval_docs,
        "paired_evidence_document_count": len(documents) - retrieval_docs,
        "query_count": len(queries),
        "paired_scenario_count": len(paired_inputs),
        "retrieval_scope_asin": RETRIEVAL_SCOPE_ASIN,
        "retrieval_splits": {
            split: sum(row["split"] == split for row in queries)
            for split in ("development", "validation", "test")
        },
        "searchable_document_fields": list(DOC_FIELDS),
        "removed_searchable_metadata": ["query_id", "topic", "qrels", "answerable", "review_id", "expected_*"],
        "runtime_context_asset": context_path.name if context_path.exists() else None,
        "runtime_context_sha256": context_hash,
        "provenance": "Synthetic runtime corpus. Search rows contain evidence text after explicit metadata cleanup; selected scope is an explicit request input kept separate from the evidence object's storage scope.",
        "files": {
            path.name: {"bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in (support_path, queries_path, paired_path)
        },
    }
    manifest_path = output_dir / "corpus_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-dir", type=Path, default=DEFAULT_PACK)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()) and not args.overwrite:
        parser.error(f"output directory is not empty: {output}; use a new version or --overwrite")
    output.mkdir(parents=True, exist_ok=True)
    print(json.dumps(build(args.pack_dir.resolve(), output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
