"""One cumulative SDK budget across classification, retrieval and investigations."""

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class RoundGuard:
    def __init__(self, run, *, paid, constructor):
        self.run, self.paid, self.constructor = Path(run), paid, constructor
        self.plan = json.loads((self.run / "plan.json").read_text())
        self.directory = self.run / ("paid" if paid else "offline")
        self.group = ""
        self.counts, self.receipts = {}, []
        self.reserved = Decimal(0)
        self.stopped = None
        self.stage_unlocked = not bool(self.plan.get("execution_stages"))
        if paid:
            approval = json.loads((self.run / "approval.json").read_text())
            if not (approval.get("approved") is True and approval.get("transfer_confirmed") is True
                    and approval.get("plan_sha256") == file_sha(self.run / "plan.json")
                    and approval.get("max_total_usd") == self.plan["budget"]["total_upper_usd"]
                    and approval.get("authorization_reference") and approval.get("processing_region")):
                raise ValueError("fresh_whole_round_budget_and_transfer_approval_required")
            preflight = json.loads((self.run / "offline/summary.json").read_text())
            if (preflight.get("status") != "offline_protocol_passed" or preflight.get("real_dispatches") != 0
                    or preflight.get("plan_sha256") != file_sha(self.run / "plan.json")):
                raise ValueError("complete_free_preflight_required")
            for name, digest in json.loads((self.run / "offline/seal.json").read_text()).items():
                if file_sha(self.run / "offline" / name) != digest:
                    raise ValueError("preflight_seal_mismatch")
        for relative, digest in self.plan["runtime_hashes"].items():
            if file_sha(self.run / relative) != digest:
                raise ValueError("frozen_candidate_mismatch")
        self.directory.mkdir()  # No redispatch or resume of an uncertain paid round.

    def client(self, **kwargs):
        if kwargs.get("max_retries") != 0 or kwargs.get("base_url") != "https://api.deepseek.com":
            raise ValueError("single_approved_supplier_no_retries_required")
        actual = self.constructor(**kwargs) if self.paid else None

        def create(**payload):
            if self.stopped:
                raise RuntimeError("whole_round_circuit_open")
            if self.paid and not self.stage_unlocked and self.group != "investigation:" + self.plan["execution_stages"]["representative_id"]:
                self.stopped = "representative_stage_locked"
                raise RuntimeError(self.stopped)
            name = payload["text"]["format"]["name"]
            kind = {"agent_planner": "planner", "evidence_relevance": "relevance", "review_semantic_extraction": "classification"}[name]
            if kind == "classification" and not self.group.startswith("classification:") or kind == "planner" and not self.group.startswith("investigation:"):
                raise ValueError("request_group_mismatch")
            if kind == "relevance" and not self.group.startswith(("retrieval:", "investigation:")):
                raise ValueError("request_group_mismatch")
            bound = self.plan["budget"]["calls"][kind]
            from app.semantic.structured_response import validate_strict_schema
            validate_strict_schema(payload["text"]["format"]["schema"])
            temperature = self.plan.get("environment", {}).get("STRUCTURED_RESPONSE_TEMPERATURE")
            if temperature is not None and payload.get("temperature") != float(temperature):
                self.stopped = "request_sampling_configuration_mismatch"
                raise ValueError(self.stopped)
            wire = json.dumps(payload, ensure_ascii=False).encode()
            if (payload["model"] != self.plan["model"] or payload["reasoning"] != {"effort": "none"}
                    or payload["text"]["format"].get("strict") is not True
                    or payload["max_output_tokens"] != bound["output_tokens"] or len(wire) + 1024 > bound["request_bytes"]):
                self.stopped = "request_preflight_failed"
                raise ValueError(self.stopped)
            group_cap = self.plan["budget"]["investigation_call_units"] if self.group.startswith("investigation:") else 1
            cap = Decimal(bound["upper_usd"])
            if (not self.group or self.counts.get(self.group, 0) >= group_cap
                    or len(self.receipts) >= self.plan["budget"]["max_calls"]
                    or self.reserved + cap > Decimal(self.plan["budget"]["total_upper_usd"])):
                self.stopped = "whole_round_budget_exhausted"
                raise ValueError(self.stopped)
            self.reserved += cap
            self.counts[self.group] = self.counts.get(self.group, 0) + 1
            index = len(self.receipts) + 1
            receipt = {"index": index, "kind": kind, "group": self.group, "request_sha256": hashlib.sha256(wire).hexdigest(),
                       "reserved_usd": str(cap), "request_bytes": len(wire), "dispatch": self.paid,
                       "charge_outcome": "unknown_until_receipt" if self.paid else "offline_substitute"}
            self.receipts.append(receipt)
            save(self.directory / f"request-{index:04}.json", payload)
            save(self.directory / f"reservation-{index:04}.json", receipt)
            if not self.paid:
                value = self.offline_payload(payload, kind)
                return SimpleNamespace(status="completed", id=f"offline-{index}", model=payload["model"], output_text=json.dumps(value),
                    usage=SimpleNamespace(input_tokens=20, output_tokens=20),
                    model_dump=lambda **kwargs: {"status": "completed", "output_text": json.dumps(value), "offline_substitute": True})
            try:
                response = actual.responses.create(**payload)
                raw = response.model_dump(mode="json")
                save(self.directory / f"response-{index:04}.json", raw)
                usage = raw.get("usage") or {}
                it, ot = usage.get("input_tokens"), usage.get("output_tokens")
                if (response.status != "completed" or type(it) is not int or type(ot) is not int
                        or min(it, ot) < 0 or it > bound["request_bytes"] or ot > bound["output_tokens"]
                        or not isinstance(raw.get("model"), str) or not raw["model"]
                        or not isinstance(raw.get("id"), str) or not raw["id"]):
                    self.stopped = "response_or_usage_incomplete"
                    raise RuntimeError(self.stopped)
                receipt.update(response_received=True, returned_model=raw.get("model"), usage=usage,
                               peak_estimate_usd=str((Decimal(it) * Decimal(".30") + Decimal(ot) * Decimal("1.20")) / 1000000))
                try:
                    json.loads(response.output_text)
                except (ValueError, TypeError) as exc:
                    self.stopped = "provider_response_json_invalid"
                    raise RuntimeError(self.stopped) from exc
                return response
            except Exception as exc:
                self.stopped = self.stopped or "provider_failure"
                save(self.directory / f"error-{index:04}.json", {"error": type(exc).__name__, "http_status": getattr(exc, "status_code", None),
                     "provider_error": getattr(exc, "body", None), "reservation_retained": True})
                raise
        return SimpleNamespace(responses=SimpleNamespace(create=create))

    def offline_payload(self, payload, kind):
        query = json.loads(payload["input"][1]["content"])
        if kind == "classification":
            return {"claims": []}
        if kind == "relevance":
            if query.get("schema_version") == "evidence-relevance-segments-v1":
                return {"request_sha256": query["request_sha256"], "decisions": [
                    {"candidate_id": c["candidate_id"], "label": "irrelevant", "evidence_segment_id": None,
                     "constraints": {key: "not_applicable" for key in query["constraints"]}} for c in query["candidates"]]}
            return {"request_sha256": query["request_sha256"], "decisions": [
                {"candidate_id": c["candidate_id"], "label": "irrelevant", "source_field": "text", "start": 0, "end": 0, "quote": "",
                 "constraints": {key: "not_applicable" for key in query["constraints"]}} for c in query["candidates"]]}
        from app.agent.output_contract import FINAL_SCHEMA_VERSION
        from insight.investigation.business_schema import (
            BUSINESS_FIELDS,
            BUSINESS_SCHEMA_VERSION,
        )
        seen = any(obs.get("data", {}).get("type") == "text_retrieval" for obs in query["observations"])
        reading = "text_evidence" in query["available_tools"] and not seen
        from app.agent.output_contract import ADDRESSED_FINAL_SCHEMA_VERSION, CURRENT_FINAL_SCHEMA_VERSION
        from insight.investigation.business_contract_registry import VERSION as CURRENT_BUSINESS_VERSION
        native = self.plan.get("environment", {}).get("INVESTIGATION_PLANNER_PROTOCOL") in {"native-v2", "native-v3"}
        return {"decision": "call_tool" if reading else "stop", "tool_name": "text_evidence" if reading else None,
                "arguments": {"query": None, "evidence_context": None} if native else "{}", "decision_summary": "Offline protocol fixture; no semantic inference", "target_hypothesis_id": None,
                "stop_reason": None if reading else "offline_protocol_stop", "confidence": None, "missing_fields": [] if native else "", "counter_evidence_query": "",
                "final_answer": None if reading else {"schema_version": ADDRESSED_FINAL_SCHEMA_VERSION if self.plan.get("environment", {}).get("INVESTIGATION_PLANNER_PROTOCOL") == "native-v3" else CURRENT_FINAL_SCHEMA_VERSION if native else FINAL_SCHEMA_VERSION, "statement": "Offline fixture, semantic quality unknown",
                    "citations": [], "business_contract": {"schema_version": CURRENT_BUSINESS_VERSION if native else BUSINESS_SCHEMA_VERSION, **{k: "unknown" for k in BUSINESS_FIELDS}}}}

    def unlock_remaining(self, trace):
        from real_review_stage_gate import representative_gate
        if trace.get("scenario_id") != self.plan["execution_stages"]["representative_id"]:
            raise ValueError("representative_identity_mismatch")
        gate = representative_gate(trace, paid=self.paid, date_scope=self.plan["date_scope"],
                                   registry_sha256=(self.plan.get("business_contract") or {}).get("registry_sha256"))
        save(self.directory / "representative-gate.json", gate)
        if self.paid and not gate["execution_gate_passed"]:
            self.stopped = self.stopped or "representative_execution_gate_failed"
            raise RuntimeError(self.stopped)
        self.stage_unlocked = True  # Offline unlock is explicitly not real execution proof.
        return gate

    def finish(self, outputs, error=None):
        if error and not self.stopped:
            self.stopped = "runtime_failure"
        summary = {"status": "partial" if error or self.stopped else "real_runtime_sealed" if self.paid else "offline_protocol_passed",
                   "real_dispatches": sum(r["dispatch"] for r in self.receipts), "captured_requests": len(self.receipts),
                   "reserved_usd": str(self.reserved), "execution_complete": not bool(error or self.stopped) and outputs == {"classification": 300, "retrieval": 30, "trajectories": 48}, "quality_accepted": False, "stopped": self.stopped, "error": error,
                   "receipts": self.receipts, "coverage": outputs, "plan_sha256": file_sha(self.run / "plan.json")}
        save(self.directory / "summary.json", summary)
        hashes = {str(p.relative_to(self.directory)): file_sha(p) for p in self.directory.rglob("*") if p.is_file()}
        save(self.directory / "seal.json", hashes)
        return summary
