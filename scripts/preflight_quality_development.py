"""Capture actual factory SDK payloads offline and calculate a proposed cost envelope."""

import argparse
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to(
        Path(".local/acceptance-artifacts").resolve()
    ):
        raise ValueError("unique local output directory required")
    import sys

    sys.path.insert(0, str(Path("backend").resolve()))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from app.agent.planner import LLMPlanner
    from app.agent.state import InvestigationState
    from app.semantic.factory import build_agent_planner, build_relevance_gateway
    from app.semantic.structured_response import structured_request
    from insight.investigation.model_limits import (
        planner_request_limits,
        validate_cost_envelope,
    )
    from insight.semantic.relevance import RelevanceAdapter

    cases = json.loads(args.inputs.read_text())["cases"]
    calls, descriptors = [], []
    decision = {
        "decision": "call_tool",
        "tool_name": "text_evidence",
        "arguments": "{}",
        "decision_summary": "Offline request capture only",
        "target_hypothesis_id": None,
        "stop_reason": None,
        "final_answer": None,
        "confidence": None,
        "missing_fields": "",
        "counter_evidence_query": "",
    }

    def create(**payload):
        calls.append(payload)
        # A capture substitute provides no model inference or quality result.
        return SimpleNamespace(
            status="completed",
            id="offline-capture",
            model=payload["model"],
            output_text=json.dumps(decision),
            usage=None,
        )

    env = {
        "DEEPSEEK_API_KEY": "nonfunctional-offline-placeholder",
        "INSIGHT_ENABLED_MODEL_PROVIDERS": "deepseek",
        "INVESTIGATION_APPROVAL_REFERENCE": "offline-not-an-approval",
        "INVESTIGATION_REGION": "no-transfer",
        "INVESTIGATION_REASONING_EFFORT": "none",
        "EVIDENCE_RELEVANCE_REASONING_EFFORT": "none",
        "INVESTIGATION_INPUT_COST_PER_MILLION": "0.30",
        "INVESTIGATION_OUTPUT_COST_PER_MILLION": "1.20",
        "INVESTIGATION_COST_CURRENCY": "USD",
        "INVESTIGATION_MAX_REQUEST_BYTES": "24000",
        "INVESTIGATION_MAX_OUTPUT_TOKENS": "3000",
        "INVESTIGATION_MAX_CALL_COST": ".1",
    }

    def capture(configuration):
        with (
            patch.dict(os.environ, configuration),
            patch(
                "openai.OpenAI",
                lambda **kw: SimpleNamespace(responses=SimpleNamespace(create=create)),
            ),
        ):
            for case in cases:
                authorized_docs = [
                    doc
                    for doc in case["documents"]
                    if doc["asin"] == case["asin"]
                    and case["start"] <= doc["date"] <= case["end"]
                ]
                for phase in ["initial", "after_evidence"]:
                    state = InvestigationState(
                        request_id="offline-development",
                        user_query=case["query"],
                        observations=[]
                        if phase == "initial"
                        else [
                            {
                                "tool_name": "text_evidence",
                                "status": "available",
                                "data": {
                                    "scope": {
                                        "asin": case["asin"],
                                        "start": case["start"],
                                        "end": case["end"],
                                    },
                                    "documents": authorized_docs,
                                },
                                "source_refs": [
                                    doc["record_id"] for doc in authorized_docs
                                ],
                            }
                        ],
                    )
                    LLMPlanner(build_agent_planner().gateway).decide(
                        state, ("text_evidence",)
                    )
                    descriptors.append(
                        {"case_id": case["case_id"], "kind": "planner", "phase": phase}
                    )
                # Adapter builds the real hashed prompt and schema. Parsing the dummy
                # capture response fails by design; this is request preflight only.
                before = len(calls)
                result = RelevanceAdapter(
                    build_relevance_gateway(),
                    max_candidates=2,
                    max_request_bytes=int(
                        configuration.get(
                            "EVIDENCE_RELEVANCE_MAX_REQUEST_BYTES", "24000"
                        )
                    ),
                    max_output_tokens=1200,
                    input_rate=".30",
                    output_rate="1.20",
                    max_cost=configuration.get("EVIDENCE_RELEVANCE_MAX_COST", ".1"),
                ).judge(
                    query=case["query"],
                    candidates=[
                        {"fact_id": doc["record_id"], "text": doc["text"]}
                        for doc in authorized_docs
                    ],
                    scope={
                        "asin": case["asin"],
                        "start": case["start"],
                        "end": case["end"],
                    },
                    complete=True,
                )
                assert result.get("error") not in {
                    "request_budget_exhausted",
                    "cost_budget_exhausted",
                }, result
                if len(calls) > before:
                    descriptors.append(
                        {
                            "case_id": case["case_id"],
                            "kind": "relevance",
                            "phase": "candidate_judgment",
                        }
                    )

    capture(env)

    bounds = {}
    for kind in ["planner", "relevance"]:
        sizes = [
            len(json.dumps(call, ensure_ascii=False).encode())
            for call, desc in zip(calls, descriptors)
            if desc["kind"] == kind
        ]
        request_cap = ((max(sizes) + 2047) // 1024) * 1024
        tokens = 3000 if kind == "planner" else 1200
        cap = (
            Decimal(request_cap) * Decimal(".30") + tokens * Decimal("1.20")
        ) / 1_000_000
        # Relevance reserves an additional 1024 bytes for transport overhead.
        if kind == "relevance":
            cap += Decimal(1024) * Decimal(".30") / 1_000_000
        bounds[kind] = {
            "max_actual_payload_bytes": max(sizes),
            "max_request_bytes": request_cap,
            "max_output_tokens": tokens,
            "max_call_usd": str(cap),
        }
    planner_cap, relevance_cap = (
        Decimal(bounds["planner"]["max_call_usd"]),
        Decimal(bounds["relevance"]["max_call_usd"]),
    )
    run_cap = 4 * max(planner_cap, relevance_cap)
    total_cap = len(cases) * run_cap + len(cases) * relevance_cap
    env.update(
        {
            "INVESTIGATION_MAX_REQUEST_BYTES": str(
                bounds["planner"]["max_request_bytes"]
            ),
            "INVESTIGATION_MAX_CALL_COST": str(planner_cap),
            "EVIDENCE_RELEVANCE_MAX_COST": str(relevance_cap),
            "INVESTIGATION_MAX_RUN_COST": str(run_cap),
            "INVESTIGATION_MAX_EVAL_COST": str(total_cap),
        }
    )
    env.update(
        {
            "EVIDENCE_RELEVANCE_MAX_REQUEST_BYTES": str(
                bounds["relevance"]["max_request_bytes"]
            ),
            "EVIDENCE_RELEVANCE_MAX_OUTPUT_TOKENS": "1200",
            "EVIDENCE_RELEVANCE_MAX_CANDIDATES": "2",
        }
    )
    calls.clear()
    descriptors.clear()
    capture(env)
    for kind, bound in bounds.items():
        bound["max_actual_payload_bytes"] = max(
            len(json.dumps(call, ensure_ascii=False).encode())
            for call, desc in zip(calls, descriptors) if desc["kind"] == kind
        )
    with patch.dict(os.environ, env):
        envelope = validate_cost_envelope(
            planner_active=True,
            relevance_active=True,
            max_steps=4,
            investigations=len(cases),
            retrieval_queries=len(cases),
        )
        for call, desc in zip(calls, descriptors):
            if desc["kind"] == "planner":
                limits = planner_request_limits(
                    call["input"][0]["content"],
                    call["input"][1]["content"],
                    call["text"]["format"]["schema"],
                )
                assert limits["max_output_tokens"] == call["max_output_tokens"]
            assert call["reasoning"]["effort"] == "none"
            # Roundtrip through the production builder guards against drift.
            assert call == structured_request(
                model=call["model"],
                effort="none",
                name=call["text"]["format"]["name"],
                system=call["input"][0]["content"],
                user=call["input"][1]["content"],
                schema=call["text"]["format"]["schema"],
                max_output_tokens=call["max_output_tokens"],
            )
    output.mkdir(parents=True)
    for index, (payload, desc) in enumerate(zip(calls, descriptors), 1):
        (output / f"request-{index:02}.json").write_text(
            json.dumps(
                {"descriptor": desc, "payload": payload}, ensure_ascii=False, indent=2
            )
            + "\n"
        )
    (output / "preflight.json").write_text(
        json.dumps(
            {
                "status": "offline_payload_preflight_passed_not_authorized",
                "input_sha256": hashlib.sha256(args.inputs.read_bytes()).hexdigest(),
                "captured_payloads": len(calls),
                "real_provider_calls": 0,
                "sdk_substitute_only": True,
                "bounds": bounds,
                "envelope": envelope,
                "prior_004_per_call_proposal_sufficient": planner_cap
                <= Decimal(".004"),
                "prior_008_whole_plan_proposal_sufficient": total_cap <= Decimal(".08"),
                "rates": "prior recorded ceiling .30/1.20 USD per million; must reconfirm in new authorization",
                "limitations": [
                    "No model inference or semantic scoring",
                    "Future trajectory prompts must pass the same bounds; unrepresented large prompts are rejected before dispatch",
                    "Shared durable ledger max_steps=4 caps planner+relevance attempts per investigation, including failures",
                    "Synthetic scoped documents; no user business transmission",
                ],
                "candidate_configuration": {
                    k: v
                    for k, v in env.items()
                    if k not in {"DEEPSEEK_API_KEY", "INVESTIGATION_APPROVAL_REFERENCE"}
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {"bounds": bounds, "envelope": envelope, "real_provider_calls": 0}, indent=2
        )
    )


if __name__ == "__main__":
    main()
