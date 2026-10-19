"""Replay original evidence/history without a provider or gold labels."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--runtime-root', type=Path, default=ROOT / 'backend')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--budget-plan', type=Path, help='Explicit candidate plan for request/cost ceiling replay')
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('new audit output required')
    run = args.run.resolve()
    plan = json.loads((run / 'plan.json').read_text())
    seals = json.loads((run / 'paid/seal.json').read_text())
    for name in ('trajectory-01.json', 'request-0003.json'):
        assert hashlib.sha256((run / 'paid' / name).read_bytes()).hexdigest() == seals[name]
    budget_plan = json.loads(args.budget_plan.read_text()) if args.budget_plan else plan
    os.environ.update(budget_plan['environment'])
    sys.path.insert(0, str(args.runtime_root.resolve()))
    from app.agent.planner import LLMPlanner
    from app.semantic.structured_response import structured_request
    from insight.investigation.model_limits import planner_request_limits
    from insight.investigation.planner import ModelPlanner
    trace = json.loads((run / 'paid/trajectory-01.json').read_text())
    original = json.loads(json.loads((run / 'paid/request-0003.json').read_text())['input'][1]['content'])
    history = [{'tool': d['next_tool'], 'tool_args': d['tool_args'], 'hypothesis': d['hypothesis'],
                'counter_evidence_query': d['counter_evidence_query'], 'stop_reason': d['stop_reason']} for d in trace['decisions']]
    captured = {}

    class Gateway:
        last_usage = {}
        last_model_receipt = {}

        def plan(self, **kw):
            wire = structured_request(model=plan['model'], effort='none', name='agent_planner',
                system=kw['system'], user=kw['user'], schema=kw['schema'], max_output_tokens=3000)
            captured.update(wire=wire, actual_wire_bytes_with_margin=len(json.dumps(wire, ensure_ascii=False).encode()) + 1024)
            try:
                planner_request_limits(kw['system'], kw['user'], kw['schema'])
                captured['preflight'] = 'passed'
            except ValueError as exc:
                captured['preflight'] = str(exc)
            raise ValueError('audit_no_dispatch')

    model = ModelPlanner.__new__(ModelPlanner)
    model.planner = LLMPlanner(Gateway())
    try:
        model.decide(query=trace['query'], evidence=trace['state_evidence'], history=history,
                     available_tools=original['available_tools'], request_context=original['frozen_scope'])
    except Exception:
        if not captured:
            raise
    cap = budget_plan['budget']['calls']['planner']['request_bytes']
    captured.update(external_calls=0, cap=cap, under_cap=captured['actual_wire_bytes_with_margin'] <= cap,
        source_run=str(run), runtime_root=str(args.runtime_root.resolve()), gold_read=False,
        evidence_sha256=hashlib.sha256(json.dumps(trace['state_evidence'], sort_keys=True).encode()).hexdigest())
    args.output.write_text(json.dumps(captured, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in captured.items() if k != 'wire'}))


if __name__ == '__main__':
    main()
