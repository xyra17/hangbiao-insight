from __future__ import annotations

from time import perf_counter
from uuid import uuid4

from ..evidence import EvidenceGuard, EvidenceInput, SourceType
from .contracts import ToolRequest, ToolResult


class EvidenceGuardTool:
    name = "evidence_guard_tool"

    def __init__(self, guard: EvidenceGuard | None = None) -> None:
        self.guard = guard or EvidenceGuard()

    def execute(self, request: ToolRequest) -> ToolResult:
        started = perf_counter()
        evidence = [EvidenceInput.model_validate(item) for item in request.params.get("evidence", [])]
        allowed = {
            (SourceType(item["source_type"]), str(item["source_id"]))
            for item in request.params.get("allowed_source_refs", [])
        }
        validation = self.guard.validate(
            claim=str(request.params.get("claim", "")),
            evidence=evidence,
            allowed_source_refs=allowed,
        )
        return ToolResult(
            tool_name=self.name,
            status="success" if validation.passed else "partial",
            data=validation.model_dump(mode="json"),
            evidence_refs=[
                item.source_id
                for item in validation.validated_evidence
                if item.source_found and item.quote_found
            ],
            warnings=[*validation.warnings, *validation.errors],
            source_version="evidence-guard-v1",
            latency_ms=round((perf_counter() - started) * 1000),
            trace_id=f"TR-{uuid4().hex}",
        )
