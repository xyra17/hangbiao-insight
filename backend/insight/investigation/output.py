"""Evidence-bound output. Model proposals remain proposals until adjudicated."""

import hashlib
import json

from .business_schema import BUSINESS_FIELDS, BUSINESS_SCHEMA_VERSION, validate_business_value
from .citations import validate_citations

OUTPUT_SCHEMA_VERSION = "investigation-output-v1"
COMPLETION_SCHEMA_VERSION = "investigation-subtasks-v1"


def compose_output(evidence, decisions):
    retrievals = [item for item in evidence if item.get("type") == "text_retrieval"]
    observations = []
    limitations = list(dict.fromkeys(text for item in retrievals for text in item.get("limitations", [])))
    for retrieval in retrievals:
        for item in retrieval.get("items", []):
            observations.append({**item, "retrieval_id": retrieval.get("id"), "phase": retrieval.get("phase"),
                                 "source_manifest": retrieval.get("source_manifest", {}),
                                 "source_kind": retrieval.get("scope", {}).get("kind"),
                                 "evidence_relation": "unknown"})
    conclusion = {"status": "unknown", "statement": "目前证据不足以形成已确认的经营结论。",
                  "citations": [], "semantic_judgment": "pending_adjudication"}
    proposals = [item for item in decisions if item.get("final_answer") or item.get("conclusion")]
    if proposals:
        decision = proposals[-1]
        proposal = decision.get("conclusion")
        conclusion["proposed_statement"] = decision.get("final_answer", "")
        conclusion["provenance"] = decision.get("planner")
        if isinstance(proposal, dict):
            conclusion["proposed_statement"] = str(proposal.get("statement", ""))
            citations = proposal.get("citations", [])
            try:
                validate_citations(citations, evidence)
                valid = bool(citations)
            except (ValueError, TypeError):
                valid = False
            if valid:
                conclusion["status"] = "pending_adjudication"
                conclusion["statement"] = "引用已回查；模型提出的解释仍需人工判断。"
                conclusion["citations"] = citations
                conclusion["citation_status"] = "verified_metric_facts" if all(cite.get("type") == "metric" for cite in citations) else "verified_mixed_evidence" if any(cite.get("type") == "metric" for cite in citations) else "verified_original_spans"
            else:
                conclusion["citation_status"] = "missing_or_invalid"
                limitations.append("模型结论未提供有效原文或指标引用，已保留为未知，不能作为已确认依据。")
    business_proposals = [item["business_contract"] for item in decisions if isinstance(item.get("business_contract"), dict)]
    business_contract = {}
    business_validation = {"status": "absent"}
    if business_proposals:
        proposal = business_proposals[-1]
        try:
            from .business_contract_registry import VERSION as CURRENT_BUSINESS_VERSION
            version = proposal.get("schema_version")
            if version not in {BUSINESS_SCHEMA_VERSION, CURRENT_BUSINESS_VERSION}:
                raise ValueError("unknown business schema")
            if version == CURRENT_BUSINESS_VERSION:
                from app.semantic.structured_response import validate_wire_value

                from .business_contract_registry import json_schema
                validate_wire_value(proposal, json_schema())
            business_contract = {"schema_version": version,
                                 **{field: validate_business_value(field, proposal[field], version=version) for field in BUSINESS_FIELDS if field in proposal}}
            business_validation = {"status": "validated", "version": version}
        except (ValueError, TypeError) as exc:
            business_validation = {"status": "rejected", "error": str(exc)[:160]}
            limitations.append("模型业务决定的格式未通过验证，缺失判断保持待裁决。")
    basis = next((item["business_basis"] for item in reversed(decisions) if item.get("business_basis")), None)
    if basis:
        try:
            validate_citations(basis.get("citations", []), evidence)
            for finding in [*basis.get("findings", []), *basis.get("subtasks", [])]:
                validate_citations(finding.get("citations", []), evidence)
        except (ValueError, TypeError):
            basis = None
            limitations.append("业务下一步的依据引用未通过校验，建议已保留为未知。")
            business_contract = {}
    # The final planner basis is authoritative for the user's requested subtasks;
    # do not infer missing business requests from arbitrary retrieved prose.
    subtasks = (basis or {}).get("subtasks", [])
    work_status = "partial" if any(item.get("status") in {"blocked", "unsupported"} for item in subtasks) and (retrievals or any(item.get("status") == "facts_available" for item in subtasks)) else "blocked" if subtasks and all(item.get("status") in {"blocked", "unsupported"} for item in subtasks) else "facts_available" if subtasks else "legacy_unspecified"
    statuses = [item.get("match_status", "legacy_unknown") for item in retrievals]
    from .business_contract_registry import VERSION as CURRENT_BUSINESS_VERSION
    from .business_contract_registry import fingerprint
    registry_identity = {"business_contract_registry_sha256": fingerprint()} if business_contract.get("schema_version") == CURRENT_BUSINESS_VERSION else {}
    return {**registry_identity, "business_schema_version": business_contract.get("schema_version", BUSINESS_SCHEMA_VERSION), "business_contract": business_contract, "business_contract_validation": business_validation,
            "business_basis": basis, "completion_schema_version": COMPLETION_SCHEMA_VERSION, "completion_status": work_status, "subtasks": subtasks, "output_schema_version": OUTPUT_SCHEMA_VERSION, "conclusion": conclusion,
            "relevance_judgments": [entry["relevance_judgment"] for entry in retrievals if entry.get("relevance_judgment", {}).get("enabled")],
            "metric_evidence": [entry["metric_evidence"] for entry in evidence if entry.get("metric_evidence")],
            "observations": observations,
            "retrieval_summary": {"related_searches": statuses.count("relevant_hits"),
                                  "context_searches": statuses.count("scope_context"),
                                  "incomplete_searches": sum(item.get("retrieval_complete") is False for item in retrievals),
                                  "empty_searches": statuses.count("no_hits"),
                                  "unknown_searches": statuses.count("legacy_unknown")},
            "limitations": limitations,
            "output_fingerprint": hashlib.sha256(json.dumps({"conclusion": conclusion, "observations": observations},
                                                             sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
