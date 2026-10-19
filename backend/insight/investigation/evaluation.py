"""Export actual traces and bind independent judgments to their exact hash. Never reads gold."""

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

from .business_contract_registry import CAPABILITY_TO_TOOL
from .business_contract_registry import VERSION as CURRENT_BUSINESS_VERSION
from .business_schema import (
    BUSINESS_FIELDS,
    BUSINESS_SCHEMA_VERSION,
    parse_review_value,
    validate_business_value,
)

ADAPTER_VERSION = "investigation-eval-adapter-v2"
SEMANTIC_FIELDS = BUSINESS_FIELDS
TOOL_SEMANTICS = {tool: capability for capability, tool in CAPABILITY_TO_TOOL.items()}
JUDGMENT_FIELDS = ("business_schema_version", "scenario_id", "trace_sha256", "reviewer", "reviewed_at", "adjudication_kind", *SEMANTIC_FIELDS)


def trace_hash(trace):
    return hashlib.sha256(json.dumps({key: value for key, value in trace.items() if key != "evaluation_metadata"}, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def load_adjudications(path, traces):
    if path is None:
        return {}
    by_id = {trace["scenario_id"]: trace for trace in traces}
    output = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not set(JUDGMENT_FIELDS) <= set(reader.fieldnames or []):
            raise ValueError("Adjudication CSV is missing required fields")
        for row in reader:
            if row["business_schema_version"] != trace_business_version(by_id.get(row["scenario_id"], {})):
                raise ValueError("Adjudication business schema version differs")
            if row["business_schema_version"] == CURRENT_BUSINESS_VERSION:
                expected = by_id.get(row["scenario_id"], {}).get("result", {}).get("business_contract_registry_sha256")
                if not expected or row.get("business_contract_registry_sha256") != expected:
                    raise ValueError("Adjudication vocabulary identity differs")
            key = row["scenario_id"]
            if key not in by_id or key in output:
                raise ValueError("Unknown or duplicate adjudication scenario")
            if row["trace_sha256"] != trace_hash(by_id[key]):
                raise ValueError("Adjudication belongs to a different runtime trace")
            if not row["reviewer"].strip() or not row["reviewed_at"].strip():
                raise ValueError("Reviewer and timestamp are required")
            stamp = datetime.fromisoformat(row["reviewed_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("Review timestamp must include timezone")
            if row["adjudication_kind"] not in {"independent_human", "controlled_fixture"}:
                raise ValueError("Adjudication provenance must be explicit")
            if not all(row[field].strip() for field in SEMANTIC_FIELDS):
                raise ValueError("Every semantic judgment must be supplied or explicitly unknown")
            for field in SEMANTIC_FIELDS:
                parse_review_value(field, row[field].strip(), version=row["business_schema_version"])
            output[key] = row
    return output


def adapt_prediction(trace, adjudication=None):
    raw_tools = [item["name"] for item in trace.get("tools", []) if item.get("status") == "completed"]
    semantic_tools = [TOOL_SEMANTICS.get(name, "unknown_tool:" + name) for name in raw_tools]
    proposed = trace.get("result", {}).get("business_contract", {})
    prediction = {"scenario_id": trace["scenario_id"],
                  **{field: "pending_adjudication:" + field for field in SEMANTIC_FIELDS}}
    if proposed.get("schema_version") in {BUSINESS_SCHEMA_VERSION, CURRENT_BUSINESS_VERSION}:
        for field in SEMANTIC_FIELDS:
            if field in proposed:
                value = validate_business_value(field, proposed[field], version=proposed["schema_version"])
                if value not in ("unknown", "pending_adjudication"):
                    prediction[field] = value
    proposal_status = "deterministic_rule_proposal" if (trace.get("result", {}).get("business_basis") or {}).get("origin") == "deterministic_tool_facts" else "model_proposal"
    if adjudication:
        for field in SEMANTIC_FIELDS:
            value = parse_review_value(field, adjudication[field].strip(), version=adjudication["business_schema_version"])
            prediction[field] = "pending_adjudication:unknown" if value in ("unknown", "pending_adjudication") else value
    return prediction, {
        "adapter_version": ADAPTER_VERSION,
        "scenario_id": trace["scenario_id"],
        "business_schema_version": trace_business_version(trace),
        "raw_terminal_status": trace.get("status"),
        "trace_sha256": trace_hash(trace),
        "raw_tool_names": raw_tools,
        "semantic_tool_names": semantic_tools,
        "raw_stop_reason": trace.get("stop_reason"),
        "actual_output": trace.get("result", {}),
        "adjudication": adjudication,
        "field_statuses": {field: "pending_adjudication" if isinstance(prediction[field], str) and prediction[field].startswith("pending_adjudication:") else "adjudicated" if adjudication else proposal_status
                           for field in SEMANTIC_FIELDS},
    }


def trace_business_version(trace):
    version = (trace.get("result", {}).get("business_contract") or {}).get("schema_version")
    if version:
        return version
    return trace.get("business_contract_version", BUSINESS_SCHEMA_VERSION)


def write_template(path, traces):
    current = any(trace_business_version(t) == CURRENT_BUSINESS_VERSION for t in traces)
    columns = (*JUDGMENT_FIELDS, "business_contract_registry_sha256", "statement_correct", "statement_notes") if current else JUDGMENT_FIELDS
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for trace in traces:
            writer.writerow({**({"business_contract_registry_sha256": trace.get("result", {}).get("business_contract_registry_sha256", ""), "statement_correct": "unknown"} if current else {}), "business_schema_version": trace_business_version(trace), "scenario_id": trace["scenario_id"], "trace_sha256": trace_hash(trace),
                             **{field: "unknown" for field in SEMANTIC_FIELDS}})

